from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts import two_step
from accounts.models import GoogleIdentity

PASSWORD = "test-password-123"


def app_code(device):
    """The code the user's authenticator app shows right now."""
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return f"{totp.token():0{device.digits}d}"


def turn_on(user):
    device = TOTPDevice.objects.create(user=user, name="Authenticator app", confirmed=True)
    static = StaticDevice.objects.create(user=user, name="Backup codes", confirmed=True)
    StaticToken.objects.create(device=static, token="backup01")
    return device


class TwoStepIsEnabledTests(TestCase):
    def test_another_users_device_does_not_turn_two_step_on_for_me(self):
        owner = get_user_model().objects.create_user(username="owner", password=PASSWORD)
        other = get_user_model().objects.create_user(username="other", password=PASSWORD)
        turn_on(owner)
        self.assertTrue(two_step.is_enabled(owner))
        self.assertFalse(two_step.is_enabled(other))


class TwoStepPasswordSignInTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="maya", password=PASSWORD)
        self.login_url = reverse("accounts:login")
        self.verify_url = reverse("accounts:two_step_verify")

    def sign_in(self, **extra):
        return self.client.post(self.login_url, {"username": "maya", "password": PASSWORD, **extra})

    def signed_in(self):
        return "_auth_user_id" in self.client.session

    def test_accounts_without_two_step_sign_in_as_before(self):
        response = self.sign_in()

        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        self.assertTrue(self.signed_in())

    def test_password_alone_is_not_enough_when_two_step_is_on(self):
        turn_on(self.user)

        response = self.sign_in()

        self.assertRedirects(response, self.verify_url, fetch_redirect_response=False)
        self.assertFalse(self.signed_in())

    def test_right_app_code_finishes_sign_in(self):
        device = turn_on(self.user)
        self.sign_in()

        response = self.client.post(self.verify_url, {"code": app_code(device)})

        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        self.assertTrue(self.signed_in())

    @override_settings(OTP_TOTP_THROTTLE_FACTOR=0)
    def test_wrong_code_keeps_the_user_out(self):
        device = turn_on(self.user)
        self.sign_in()

        response = self.client.post(self.verify_url, {"code": "000000" if app_code(device) != "000000" else "111111"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That code didn’t work")
        self.assertFalse(self.signed_in())

    @override_settings(OTP_TOTP_THROTTLE_FACTOR=0)
    def test_five_wrong_codes_end_the_attempt(self):
        turn_on(self.user)
        self.sign_in()

        for _ in range(two_step.MAX_ATTEMPTS):
            response = self.client.post(self.verify_url, {"code": "wrong"})

        self.assertRedirects(response, self.login_url, fetch_redirect_response=False)
        self.assertNotIn(two_step.PENDING_KEY, self.client.session)

    def test_backup_code_works_once(self):
        turn_on(self.user)
        self.sign_in()
        self.assertRedirects(self.client.post(self.verify_url, {"code": "backup01"}),
                             reverse("accounts:dashboard"), fetch_redirect_response=False)

        self.client.logout()
        self.sign_in()
        response = self.client.post(self.verify_url, {"code": "backup01"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.signed_in())

    def test_code_step_expires(self):
        device = turn_on(self.user)
        self.sign_in()
        session = self.client.session
        session[two_step.PENDING_KEY]["started"] -= two_step.PENDING_MAX_AGE + 1
        session.save()

        response = self.client.post(self.verify_url, {"code": app_code(device)})

        self.assertRedirects(response, self.login_url, fetch_redirect_response=False)
        self.assertFalse(self.signed_in())

    def test_code_step_without_a_password_first_is_refused(self):
        response = self.client.get(self.verify_url)

        self.assertRedirects(response, self.login_url, fetch_redirect_response=False)

    def test_safe_next_is_kept_and_unsafe_next_is_dropped(self):
        device = turn_on(self.user)
        self.sign_in(next=reverse("accounts:profile"))
        response = self.client.post(self.verify_url, {"code": app_code(device)})
        self.assertRedirects(response, reverse("accounts:profile"), fetch_redirect_response=False)

        self.client.logout()
        self.sign_in(next="https://evil.example/steal")
        session = self.client.session
        session[two_step.PENDING_KEY]["next"] = "https://evil.example/steal"
        session.save()
        response = self.client.post(self.verify_url, {"code": "backup01"})
        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)


@override_settings(GOOGLE_CLIENT_ID="client-id", GOOGLE_CLIENT_SECRET="client-secret")
class TwoStepGoogleSignInTests(TestCase):
    def test_google_sign_in_also_asks_for_the_code(self):
        user = get_user_model().objects.create_user(username="sam", email="sam@gmail.com")
        GoogleIdentity.objects.create(user=user, subject="google-1", email="sam@gmail.com")
        user.last_login = user.date_joined
        user.save()
        device = turn_on(user)
        with patch("accounts.views.google.login_authorization_url",
                   return_value=("https://accounts.google.com/o/oauth2/auth?x=1", "state-123", "verifier")):
            self.client.post(reverse("accounts:google_login"))
        claims = {"sub": "google-1", "email": "sam@gmail.com", "email_verified": True}
        with patch("accounts.views.google.verify_login", return_value=claims):
            response = self.client.get(reverse("accounts:google_login_callback"), {"state": "state-123", "code": "c"})

        self.assertRedirects(response, reverse("accounts:two_step_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self.client.post(reverse("accounts:two_step_verify"), {"code": app_code(device)})
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)


class TwoStepSetupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="maya", password=PASSWORD)
        self.client.force_login(self.user)
        self.setup_url = reverse("accounts:two_step_setup")

    def test_setup_shows_a_qr_code_and_is_not_on_until_confirmed(self):
        response = self.client.get(self.setup_url)

        self.assertContains(response, "<svg")
        self.assertFalse(two_step.is_enabled(self.user))
        self.assertEqual(TOTPDevice.objects.filter(user=self.user, confirmed=False).count(), 1)

    def test_reloading_setup_keeps_the_same_secret(self):
        self.client.get(self.setup_url)
        key = TOTPDevice.objects.get(user=self.user).key
        self.client.get(self.setup_url)

        self.assertEqual(TOTPDevice.objects.get(user=self.user).key, key)

    def test_right_code_turns_it_on_and_shows_ten_backup_codes_once(self):
        self.client.get(self.setup_url)
        device = TOTPDevice.objects.get(user=self.user)

        response = self.client.post(self.setup_url, {"code": app_code(device)})

        self.assertTrue(two_step.is_enabled(self.user))
        self.assertEqual(len(response.context["codes"]), two_step.BACKUP_CODE_COUNT)
        self.assertEqual(two_step.backup_codes_left(self.user), two_step.BACKUP_CODE_COUNT)

    def test_wrong_code_leaves_it_off(self):
        self.client.get(self.setup_url)

        response = self.client.post(self.setup_url, {"code": "123456"})

        self.assertContains(response, "That code didn’t work")
        self.assertFalse(two_step.is_enabled(self.user))

    @override_settings(OTP_TOTP_THROTTLE_FACTOR=0)  # a wrong code otherwise locks the device for 1s
    def test_turning_off_needs_a_valid_code(self):
        device = turn_on(self.user)
        self.client.post(reverse("accounts:two_step_disable"), {"code": "nope"})
        self.assertTrue(two_step.is_enabled(self.user))

        self.client.post(reverse("accounts:two_step_disable"), {"code": app_code(device)})
        self.assertFalse(two_step.is_enabled(self.user))
        self.assertFalse(StaticDevice.objects.filter(user=self.user).exists())

    def test_profile_shows_the_current_state(self):
        self.assertContains(self.client.get(reverse("accounts:profile")), "Turn on two-step sign-in")
        turn_on(self.user)
        self.assertContains(self.client.get(reverse("accounts:profile")), "Turn off two-step sign-in")
