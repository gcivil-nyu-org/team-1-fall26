from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import GoogleIdentity

PASSWORD = "BlueKite2026!"


class SignUpTests(TestCase):
    def setUp(self):
        self.url = reverse("accounts:signup")

    def sign_up(self, **overrides):
        data = {
            "username": "maya",
            "email": "Maya@Example.com",
            "password1": PASSWORD,
            "password2": PASSWORD,
            **overrides,
        }
        return self.client.post(self.url, data)

    def test_new_account_is_created_and_signed_in(self):
        response = self.sign_up()

        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        user = get_user_model().objects.get(username="maya")
        self.assertEqual(user.email, "maya@example.com")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_new_account_without_a_family_is_sent_to_create_one(self):
        self.sign_up()

        response = self.client.get(reverse("accounts:dashboard"))

        self.assertRedirects(response, reverse("accounts:create_workspace"))

    def test_weak_or_mismatched_passwords_are_rejected(self):
        for overrides in (
            {"password1": "123456", "password2": "123456"},
            {"password2": "Different2026!"},
            {"password1": "maya", "password2": "maya"},
        ):
            with self.subTest(overrides=overrides):
                response = self.sign_up(**overrides)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(get_user_model().objects.exists())

    def test_taken_username_or_email_is_rejected(self):
        get_user_model().objects.create_user(username="Maya", email="maya@example.com", password=PASSWORD)

        self.assertEqual(self.sign_up(email="other@example.com").status_code, 200)
        response = self.sign_up(username="maya2")
        self.assertContains(response, "already exists")
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_signed_in_visitors_go_to_their_dashboard(self):
        user = get_user_model().objects.create_user(username="maya", password=PASSWORD)
        self.client.force_login(user)

        response = self.client.get(self.url)

        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)

    def test_login_page_links_to_sign_up(self):
        self.assertContains(self.client.get(reverse("accounts:login")), self.url)


@override_settings(GOOGLE_CLIENT_ID="client-id", GOOGLE_CLIENT_SECRET="client-secret")
class GoogleSignInTests(TestCase):
    def setUp(self):
        self.start_url = reverse("accounts:google_login")
        self.callback_url = reverse("accounts:google_login_callback")

    def start(self):
        with patch(
            "accounts.views.google.login_authorization_url",
            return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state-123", "verifier"),
        ):
            return self.client.post(self.start_url)

    def finish(self, claims, state="state-123"):
        with patch("accounts.views.google.verify_login", return_value=claims) as verify:
            response = self.client.get(self.callback_url, {"state": state, "code": "auth-code"})
        return response, verify

    def claims(self, **overrides):
        return {"sub": "google-1", "email": "Sam.Lee@gmail.com", "email_verified": True, **overrides}

    def test_start_sends_the_visitor_to_google(self):
        response = self.start()

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("https://accounts.google.com/"))

    def test_first_sign_in_creates_an_account_without_a_password(self):
        self.start()
        response, verify = self.finish(self.claims())

        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        user = get_user_model().objects.get()
        self.assertEqual(user.username, "Sam.Lee")
        self.assertEqual(user.email, "sam.lee@gmail.com")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.google_identity.subject, "google-1")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.assertTrue(verify.call_args.args[0].endswith(self.callback_url))

    def test_returning_google_account_signs_in_to_the_same_account(self):
        self.start()
        self.finish(self.claims())
        self.client.logout()

        self.start()
        # Same Google account, even after changing its email address.
        self.finish(self.claims(email="sam@newmail.com"))

        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(int(self.client.session["_auth_user_id"]), get_user_model().objects.get().pk)

    def test_username_gets_a_number_when_taken(self):
        get_user_model().objects.create_user(username="sam.lee", email="other@example.com", password=PASSWORD)
        self.start()
        self.finish(self.claims())

        self.assertTrue(get_user_model().objects.filter(username="Sam.Lee2").exists())

    def test_google_never_takes_over_an_existing_password_account(self):
        owner = get_user_model().objects.create_user(username="sam", email="sam.lee@gmail.com", password=PASSWORD)
        self.start()
        response, _ = self.finish(self.claims())

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(GoogleIdentity.objects.exists())
        self.assertEqual(get_user_model().objects.get().pk, owner.pk)

    def test_unverified_google_email_is_refused(self):
        self.start()
        response, _ = self.finish(self.claims(email_verified=False))

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertFalse(get_user_model().objects.exists())

    def test_wrong_or_missing_state_is_refused(self):
        self.start()
        response, verify = self.finish(self.claims(), state="forged")
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        verify.assert_not_called()

        # The state is single-use, so replaying the right one later fails too.
        response, verify = self.finish(self.claims())
        verify.assert_not_called()
        self.assertFalse(get_user_model().objects.exists())

    def test_start_needs_post(self):
        self.assertEqual(self.client.get(self.start_url).status_code, 405)

    @override_settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")
    def test_button_hidden_and_start_refused_without_google_setup(self):
        response = self.client.post(self.start_url)
        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertNotContains(self.client.get(reverse("accounts:signup")), "Continue with Google")


@override_settings(GOOGLE_CLIENT_ID="client-id", GOOGLE_CLIENT_SECRET="client-secret")
class ConnectGoogleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="simon", email="simon@example.com", password=PASSWORD,
        )
        self.profile_url = reverse("accounts:profile")
        self.callback_url = reverse("accounts:google_login_callback")
        self.client.force_login(self.user)

    def connect(self, claims=None, start_as=None):
        with patch(
            "accounts.views.google.login_authorization_url",
            return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state-123", "verifier"),
        ):
            self.client.post(reverse("accounts:google_connect"))
        if start_as is not None:
            self.client.force_login(start_as)
        claims = claims or {"sub": "google-7", "email": "Simon.Personal@gmail.com", "email_verified": True}
        with patch("accounts.views.google.verify_login", return_value=claims):
            return self.client.get(self.callback_url, {"state": "state-123", "code": "auth-code"})

    def sign_in_with_google(self, sub="google-7"):
        self.client.logout()
        with patch(
            "accounts.views.google.login_authorization_url",
            return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state-9", "verifier"),
        ):
            self.client.post(reverse("accounts:google_login"))
        claims = {"sub": sub, "email": "simon.personal@gmail.com", "email_verified": True}
        with patch("accounts.views.google.verify_login", return_value=claims):
            self.client.get(self.callback_url, {"state": "state-9", "code": "auth-code"})

    def test_existing_account_can_connect_google_and_then_sign_in_with_it(self):
        response = self.connect()

        self.assertRedirects(response, self.profile_url, fetch_redirect_response=False)
        self.assertEqual(self.user.google_identity.subject, "google-7")
        self.sign_in_with_google()
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_connecting_works_even_when_the_google_email_differs(self):
        self.connect({"sub": "google-7", "email": "someone.else@gmail.com", "email_verified": True})
        self.assertEqual(self.user.google_identity.email, "someone.else@gmail.com")

    def test_a_google_account_cannot_be_connected_to_two_accounts(self):
        other = get_user_model().objects.create_user(username="other", password=PASSWORD)
        GoogleIdentity.objects.create(user=other, subject="google-7", email="x@gmail.com")

        response = self.connect()

        self.assertRedirects(response, self.profile_url, fetch_redirect_response=False)
        self.assertFalse(GoogleIdentity.objects.filter(user=self.user).exists())
        self.assertEqual(GoogleIdentity.objects.get(subject="google-7").user, other)

    def test_connect_finishes_only_for_the_account_that_started_it(self):
        other = get_user_model().objects.create_user(username="other", password=PASSWORD)

        response = self.connect(start_as=other)

        self.assertRedirects(response, reverse("accounts:login"), fetch_redirect_response=False)
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_connect_needs_a_signed_in_user(self):
        self.client.logout()
        response = self.client.post(reverse("accounts:google_connect"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("accounts:login")))

    def test_disconnect_turns_google_sign_in_off(self):
        self.connect()

        response = self.client.post(reverse("accounts:google_disconnect"))

        self.assertRedirects(response, self.profile_url, fetch_redirect_response=False)
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_google_only_account_must_set_a_password_before_disconnecting(self):
        self.user.set_unusable_password()
        self.user.save()
        # Changing the password ends existing sessions, as it does for real users.
        self.client.force_login(self.user)
        self.connect()

        self.client.post(reverse("accounts:google_disconnect"))
        self.assertTrue(GoogleIdentity.objects.exists())

        response = self.client.post(self.profile_url, {
            "action": "password", "new_password1": "NewKite2026!", "new_password2": "NewKite2026!",
        })
        self.assertRedirects(response, self.profile_url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewKite2026!"))
        self.client.post(reverse("accounts:google_disconnect"))
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_profile_offers_connect_then_shows_the_connected_account(self):
        self.assertContains(self.client.get(self.profile_url), "Connect Google")
        self.connect()
        self.assertContains(self.client.get(self.profile_url), "simon.personal@gmail.com")
