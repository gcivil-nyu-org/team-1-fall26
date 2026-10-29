from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

PASSWORD = "FamilyTest2026!"


class LoginLockoutTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.parent = User.objects.create_user(
            username="LockoutParent", password=PASSWORD
        )
        self.kid = User.objects.create_user(username="LockoutKid", password=PASSWORD)
        self.login_url = reverse("accounts:login")

    def attempt(self, username, password, ip="203.0.113.7", forwarded_for=None):
        # nginx appends the address it sees, so the client's own value comes first.
        chain = f"{forwarded_for}, {ip}" if forwarded_for else ip
        return self.client.post(
            self.login_url,
            {"username": username, "password": password},
            HTTP_X_FORWARDED_FOR=chain,
        )

    def fail(self, times, **kwargs):
        for _ in range(times):
            response = self.attempt(self.parent.username, "wrong-password", **kwargs)
            self.assertIn(response.status_code, (200, 429))

    def test_five_wrong_passwords_lock_out_even_the_right_one(self):
        self.fail(4)
        response = self.attempt(self.parent.username, "wrong-password")
        self.assertEqual(response.status_code, 429)
        response = self.attempt(self.parent.username, PASSWORD)
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many sign-in attempts", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_successful_login_resets_the_count(self):
        self.fail(4)
        self.assertEqual(self.attempt(self.parent.username, PASSWORD).status_code, 302)
        self.client.logout()
        self.fail(4)
        self.assertEqual(self.attempt(self.parent.username, PASSWORD).status_code, 302)

    def test_lockout_does_not_spread_to_family_on_the_same_network(self):
        self.fail(5)
        self.assertEqual(self.attempt(self.kid.username, PASSWORD).status_code, 302)

    def test_same_username_from_another_network_can_still_sign_in(self):
        self.fail(5)
        self.assertEqual(
            self.attempt(
                self.parent.username, PASSWORD, ip="198.51.100.20"
            ).status_code,
            302,
        )

    def test_a_forged_forwarded_for_header_does_not_dodge_the_lockout(self):
        for index in range(5):
            self.attempt(
                self.parent.username, "wrong-password", forwarded_for=f"192.0.2.{index}"
            )
        response = self.attempt(
            self.parent.username, PASSWORD, forwarded_for="192.0.2.99"
        )
        self.assertEqual(response.status_code, 429)

    def test_admin_login_is_protected_too(self):
        admin_login = reverse("admin:login")
        for _ in range(5):
            self.client.post(
                admin_login,
                {"username": self.parent.username, "password": "wrong-password"},
                HTTP_X_FORWARDED_FOR="203.0.113.7",
            )
        response = self.attempt(self.parent.username, PASSWORD)
        self.assertEqual(response.status_code, 429)


class AdminPathTests(TestCase):
    def test_admin_lives_at_the_configured_path(self):
        self.assertEqual(reverse("admin:index"), "/" + settings.ADMIN_PATH)
