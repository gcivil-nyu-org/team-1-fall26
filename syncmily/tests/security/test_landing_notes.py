"""Security lane "landing": the part of the landing-page audit that touches the main app.

The standalone landing repo (/Users/simonsavine/syncmily-landing) has its own PoC suite in the
scratch copy (waitlist/test_poc_security.py). This module covers the one cross-system finding.
Every test_poc_* asserts the SECURE behaviour, so it FAILS while the bug exists.
Run:
  POSTGRES_DB=syncmily_sec_landing python manage.py test syncmily.tests.security.test_landing_notes
No network: nothing here leaves the process.
"""

import unittest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse


class WaitlistGateTests(TestCase):
    def setUp(self):
        cache.clear()

    # Open decision: whether app sign-up waits for waitlist approval. Audit REPORT.md (h).
    @unittest.expectedFailure
    def test_poc_LANDING_010_signup_is_gated_by_the_waitlist_approval(self):
        """LANDING-010: the landing page's 'approved' state is cosmetic. Its Open Syncmily button goes to
        /accounts/signup/, which the app opens to anyone with no token from the waitlist.
        """
        before = get_user_model().objects.count()
        response = Client().post(
            reverse("accounts:signup"),
            {
                "username": "uninvited-poc",
                "email": "uninvited-poc@example.test",
                "password1": "Zq-7Tr-local-poc-91",
                "password2": "Zq-7Tr-local-poc-91",
            },
        )
        created = get_user_model().objects.count() - before
        self.assertEqual(
            created,
            0,
            f"anonymous visitor with no waitlist token created {created} account(s); status {response.status_code}",
        )
