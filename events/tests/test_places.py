from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Workspace

from .. import places, views

PLACE_ID = "ChIJmQJIxlVYwokRLgeuocVOGVU"


def fake_response(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


SUGGESTIONS = {
    "suggestions": [
        {
            "placePrediction": {
                "placeId": PLACE_ID,
                "text": {"text": "Times Square, Manhattan, NY, USA"},
                "structuredFormat": {
                    "mainText": {"text": "Times Square"},
                    "secondaryText": {"text": "Manhattan, NY, USA"},
                },
            }
        },
        {"queryPrediction": {"text": {"text": "times square hotels"}}},
    ]
}
DETAILS = {
    "id": PLACE_ID,
    "displayName": {"text": "Times Square"},
    "formattedAddress": "Manhattan, NY 10036, USA",
    "location": {"latitude": 40.758, "longitude": -73.9855},
}


class PlacesSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="Ana", password="FamilyTest2026!"
        )

    def setUp(self):
        cache.clear()
        # Only members of a family search places.
        Workspace.objects.get_or_create(name="Ana family", primary_admin=self.user)
        self.client.force_login(self.user)

    @override_settings(GOOGLE_MAPS_API_KEY="")
    def test_without_a_key_the_box_falls_back_to_free_search(self):
        response = self.client.post(reverse("events:place_suggestions"), {"q": "Times"})

        self.assertEqual(response.status_code, 503)

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    def test_suggestions_come_from_google_with_an_nyc_bias(self):
        with mock.patch.object(
            places.requests, "post", return_value=fake_response(SUGGESTIONS)
        ) as post:
            response = self.client.post(
                reverse("events:place_suggestions"),
                {"q": "Times Sq", "session": "abc-123"},
            )

        self.assertEqual(
            response.json()["suggestions"],
            [
                {
                    "id": PLACE_ID,
                    "main": "Times Square",
                    "secondary": "Manhattan, NY, USA",
                }
            ],
        )
        sent = post.call_args.kwargs
        self.assertEqual(sent["headers"]["X-Goog-Api-Key"], "test-key")
        self.assertEqual(sent["json"]["sessionToken"], "abc-123")
        self.assertEqual(sent["json"]["locationBias"], places.NYC_BIAS)

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    def test_picking_a_place_gives_its_exact_coordinates(self):
        with mock.patch.object(
            places.requests, "get", return_value=fake_response(DETAILS)
        ) as get:
            response = self.client.post(
                reverse("events:place_details"), {"id": PLACE_ID, "session": "abc-123"}
            )

        self.assertEqual(
            response.json(),
            {
                "label": "Times Square, Manhattan, NY 10036, USA",
                "latitude": 40.758,
                "longitude": -73.9855,
            },
        )
        sent = get.call_args.kwargs
        self.assertEqual(sent["headers"]["X-Goog-FieldMask"], places.DETAILS_FIELDS)
        self.assertEqual(sent["params"], {"sessionToken": "abc-123"})

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    def test_short_queries_and_bad_place_ids(self):
        with mock.patch.object(places.requests, "post") as post:
            short = self.client.post(reverse("events:place_suggestions"), {"q": "Ti"})
        post.assert_not_called()
        self.assertEqual(short.json(), {"suggestions": []})
        bad = self.client.post(reverse("events:place_details"), {"id": "../x"})
        self.assertEqual(bad.status_code, 400)

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    def test_google_outage_falls_back(self):
        with mock.patch.object(
            places.requests, "post", side_effect=requests.ConnectionError
        ):
            response = self.client.post(
                reverse("events:place_suggestions"), {"q": "Times"}
            )

        self.assertEqual(response.status_code, 503)

    @override_settings(GOOGLE_MAPS_API_KEY="test-key", RATELIMIT_ENABLED=True)
    def test_searches_are_rate_limited(self):
        with mock.patch.object(
            views, "PLACE_SEARCHES_PER_MINUTE", 2
        ), mock.patch.object(
            places.requests, "post", return_value=fake_response(SUGGESTIONS)
        ):
            codes = [
                self.client.post(
                    reverse("events:place_suggestions"), {"q": "Times"}
                ).status_code
                for _ in range(3)
            ]

        self.assertEqual(codes, [200, 200, 429])

    @override_settings(GOOGLE_MAPS_API_KEY="test-key")
    def test_needs_sign_in_and_post(self):
        self.assertEqual(
            self.client.get(reverse("events:place_suggestions")).status_code, 405
        )
        self.client.logout()
        response = self.client.post(reverse("events:place_suggestions"), {"q": "Times"})
        self.assertEqual(response.status_code, 302)

    @override_settings(GOOGLE_MAPS_API_KEY="secret-key")
    def test_the_key_never_reaches_the_browser(self):
        with mock.patch.object(
            places.requests, "post", return_value=fake_response(SUGGESTIONS)
        ):
            response = self.client.post(
                reverse("events:place_suggestions"), {"q": "Times"}
            )

        self.assertNotIn(b"secret-key", response.content)
