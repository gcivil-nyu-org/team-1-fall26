
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from .models import Membership, Workspace
from .services import (
    accept_invitation,
    invite_member,
    make_invite_token,
    next_member_username,
)


NEW_PASSWORD = "BlueKite2026!"


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


class MemberInvitationTests(TestCase):

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="Parent1",
            password="TestPassword123!",
        )
        self.workspace = Workspace.objects.create(
            name="Smith Family",
            primary_admin=self.admin,
        )
        self.members_url = reverse(
            "accounts:manage_members",
            kwargs={"workspace_id": self.workspace.id},
        )

    def invite(self, email, role):
        return invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email=email,
            role=role,
        )

    def test_primary_admin_can_invite_kid(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            self.members_url,
            {"email": "kid@example.com", "role": "kid"},
        )

        self.assertEqual(response.status_code, 302)
        membership = Membership.objects.get()
        self.assertEqual(membership.user.username, "Kid1")
        self.assertEqual(membership.role, Membership.Role.KID)
        self.assertFalse(membership.user.is_active)
        self.assertFalse(membership.user.has_usable_password())

    def test_invitation_email_is_sent(self):
        self.client.force_login(self.admin)

        self.client.post(
            self.members_url,
            {"email": "parent@example.com", "role": "parent"},
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["parent@example.com"])
        self.assertIn("Parent2", mail.outbox[0].body)
        self.assertIn("/accounts/invite/", mail.outbox[0].body)

    def test_usernames_are_numbered_per_role(self):
        self.invite("a@example.com", Membership.Role.PARENT)
        self.invite("b@example.com", Membership.Role.KID)
        self.invite("c@example.com", Membership.Role.KID)
        self.invite("d@example.com", Membership.Role.PARENT)

        usernames = set(
            Membership.objects.values_list("user__username", flat=True)
        )
        self.assertEqual(usernames, {"Parent2", "Parent3", "Kid1", "Kid2"})

    def test_renamed_member_frees_up_number(self):
        get_user_model().objects.create_user(username="kid1", password="x")

        self.assertEqual(next_member_username(Membership.Role.KID), "Kid2")

    def test_other_parent_cannot_invite(self):
        parent = self.invite("p@example.com", Membership.Role.PARENT).user
        parent.is_active = True
        parent.save()
        self.client.force_login(parent)

        response = self.client.post(
            self.members_url,
            {"email": "kid@example.com", "role": "kid"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Membership.objects.count(), 1)

    def test_duplicate_email_in_workspace_is_rejected(self):
        self.invite("kid@example.com", Membership.Role.KID)
        self.client.force_login(self.admin)

        response = self.client.post(
            self.members_url,
            {"email": "KID@example.com", "role": "kid"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Membership.objects.count(), 1)
        self.assertContains(response, "already in your workspace")

    def test_member_cannot_sign_in_before_confirming(self):
        membership = self.invite("kid@example.com", Membership.Role.KID)

        logged_in = self.client.login(
            username=membership.user.username,
            password="123456",
        )

        self.assertFalse(logged_in)
        self.assertFalse(membership.user.has_usable_password())

    def test_opening_invite_link_does_not_activate(self):
        membership = self.invite("kid@example.com", Membership.Role.KID)

        response = self.client.get(
            reverse(
                "accounts:accept_invite",
                kwargs={"token": make_invite_token(membership)},
            )
        )

        self.assertContains(response, "Kid1")
        membership.refresh_from_db()
        self.assertTrue(membership.is_pending)

    def test_confirming_invite_sets_the_chosen_password_and_signs_in(self):
        membership = self.invite("kid@example.com", Membership.Role.KID)

        response = self.client.post(
            reverse(
                "accounts:accept_invite",
                kwargs={"token": make_invite_token(membership)},
            ),
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )

        self.assertRedirects(response, reverse("accounts:dashboard"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), membership.user.pk)
        membership.refresh_from_db()
        self.assertFalse(membership.is_pending)
        self.client.logout()
        self.assertTrue(self.client.login(username="Kid1", password=NEW_PASSWORD))

    def test_confirming_invite_needs_a_valid_password(self):
        membership = self.invite("kid@example.com", Membership.Role.KID)
        accept_url = reverse(
            "accounts:accept_invite",
            kwargs={"token": make_invite_token(membership)},
        )

        for data in (
            {"new_password1": NEW_PASSWORD, "new_password2": "Different2026!"},
            {"new_password1": "123456", "new_password2": "123456"},
            {},
        ):
            with self.subTest(data=data):
                response = self.client.post(accept_url, data)
                self.assertEqual(response.status_code, 200)
                membership.refresh_from_db()
                self.assertTrue(membership.is_pending)
                self.assertFalse(membership.user.has_usable_password())

    def test_tampered_invite_token_is_rejected(self):
        response = self.client.get(
            reverse(
                "accounts:accept_invite",
                kwargs={"token": "not-a-real-token"},
            )
        )

        self.assertEqual(response.status_code, 400)


class DashboardTests(TestCase):

    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="Parent1",
            password="TestPassword123!",
        )
        self.workspace = Workspace.objects.create(
            name="Smith Family",
            primary_admin=self.admin,
        )
        self.dashboard_url = reverse("accounts:dashboard")

    def member(self, role):
        membership = invite_member(
            workspace=self.workspace,
            invited_by=self.admin,
            email=f"{role}@example.com",
            role=role,
        )
        accept_invitation(membership)
        return membership.user

    def test_primary_admin_sees_parent_dashboard(self):
        self.client.force_login(self.admin)

        response = self.client.get(self.dashboard_url)

        self.assertTemplateUsed(response, "accounts/parent_dashboard.html")
        self.assertContains(response, "Add a family member")

    def test_other_parent_sees_parent_dashboard_without_invite(self):
        self.client.force_login(self.member(Membership.Role.PARENT))

        response = self.client.get(self.dashboard_url)

        self.assertTemplateUsed(response, "accounts/parent_dashboard.html")
        self.assertNotContains(response, "Add a family member")

    def test_kid_sees_kid_dashboard(self):
        self.client.force_login(self.member(Membership.Role.KID))

        response = self.client.get(self.dashboard_url)

        self.assertTemplateUsed(response, "accounts/kid_dashboard.html")
        self.assertContains(response, "Smith Family")

    def test_user_without_workspace_is_sent_to_create_one(self):
        user = get_user_model().objects.create_user(
            username="loner",
            password="TestPassword123!",
        )
        self.client.force_login(user)

        response = self.client.get(self.dashboard_url)

        self.assertRedirects(response, reverse("accounts:create_workspace"))

    def test_sign_in_redirects_to_dashboard(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "Parent1", "password": "TestPassword123!"},
        )

        self.assertRedirects(response, self.dashboard_url)


class ProfileTests(TestCase):

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="Kid1",
            password="StartPass2026!",
        )
        self.profile_url = reverse("accounts:profile")
        self.client.force_login(self.user)

    def test_user_can_change_username(self):
        response = self.client.post(
            self.profile_url,
            {"action": "username", "username": "Maya"},
        )

        self.assertRedirects(response, self.profile_url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "Maya")

    def test_taken_username_is_rejected(self):
        get_user_model().objects.create_user(username="Maya", password="x")

        response = self.client.post(
            self.profile_url,
            {"action": "username", "username": "maya"},
        )

        self.assertContains(response, "already taken")
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "Kid1")

    def test_user_can_change_password_and_stays_signed_in(self):
        response = self.client.post(
            self.profile_url,
            {
                "action": "password",
                "old_password": "StartPass2026!",
                "new_password1": "BlueKite2026!",
                "new_password2": "BlueKite2026!",
            },
        )

        self.assertRedirects(response, self.profile_url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BlueKite2026!"))
        self.assertEqual(self.client.get(self.profile_url).status_code, 200)

    def test_wrong_current_password_is_rejected(self):
        response = self.client.post(
            self.profile_url,
            {
                "action": "password",
                "old_password": "wrong",
                "new_password1": "BlueKite2026!",
                "new_password2": "BlueKite2026!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("StartPass2026!"))


class HomeRedirectTests(TestCase):
    def test_bare_domain_leads_signed_out_visitors_to_login(self):
        response = self.client.get("/", follow=True)
        self.assertEqual(response.redirect_chain[0][0], reverse("accounts:dashboard"))
        self.assertEqual(response.request["PATH_INFO"], reverse("accounts:login"))
