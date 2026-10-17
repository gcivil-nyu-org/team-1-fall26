
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from .models import Workspace


class WorkspaceCreationTests(TestCase):

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="testadmin",
            password="TestPassword123!",
        )
        self.other_user = User.objects.create_user(
            username="otheruser",
            password="TestPassword123!",
        )
        self.create_url = reverse("accounts:create_workspace")

    def test_authenticated_user_can_create_workspace(self):
        self.client.force_login(self.user)

        response = self.client.post(
            self.create_url,
            {"name": "Smith Family"},
        )

        self.assertEqual(Workspace.objects.count(), 1)
        self.assertEqual(response.status_code, 302)

    def test_workspace_has_unique_identifier(self):
        self.client.force_login(self.user)

        self.client.post(self.create_url, {"name": "Family One"})
        self.client.post(self.create_url, {"name": "Family Two"})

        workspaces = list(Workspace.objects.all())

        self.assertEqual(len(workspaces), 2)
        self.assertNotEqual(workspaces[0].id, workspaces[1].id)

    def test_creator_is_primary_admin(self):
        self.client.force_login(self.user)

        self.client.post(
            self.create_url,
            {"name": "Smith Family"},
        )

        workspace = Workspace.objects.get(name="Smith Family")
        self.assertEqual(workspace.primary_admin, self.user)

    def test_creator_can_access_workspace(self):
        self.client.force_login(self.user)

        workspace = Workspace.objects.create(
            name="Smith Family",
            primary_admin=self.user,
        )

        response = self.client.get(
            reverse(
                "accounts:workspace_detail",
                kwargs={"workspace_id": workspace.id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Smith Family")

    def test_other_user_cannot_access_workspace(self):
        workspace = Workspace.objects.create(
            name="Private Family",
            primary_admin=self.user,
        )

        self.client.force_login(self.other_user)

        response = self.client.get(
            reverse(
                "accounts:workspace_detail",
                kwargs={"workspace_id": workspace.id},
            )
        )

        self.assertEqual(response.status_code, 403)

    def test_empty_workspace_name_is_rejected(self):
        self.client.force_login(self.user)

        response = self.client.post(
            self.create_url,
            {"name": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Workspace.objects.count(), 0)
        self.assertContains(response, "required")

    def test_whitespace_workspace_name_is_rejected(self):
        self.client.force_login(self.user)

        response = self.client.post(
            self.create_url,
            {"name": "   "},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Workspace.objects.count(), 0)
        self.assertContains(response, "required")

    def test_anonymous_user_cannot_create_workspace(self):
        response = self.client.post(
            self.create_url,
            {"name": "Unauthorized Workspace"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Workspace.objects.count(), 0)

    def test_database_failure_shows_error(self):
        self.client.force_login(self.user)

        with patch.object(
            Workspace,
            "save",
            side_effect=IntegrityError("Database failure"),
        ):
            response = self.client.post(
                self.create_url,
                {"name": "Smith Family"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Workspace.objects.count(), 0)
        self.assertContains(
            response,
            "Workspace creation failed. Please try again.",
        )
