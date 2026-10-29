from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from calendars.models import CalendarConnection
from chat.models import ChatMessage
from checklists.models import Checklist
from events.models import Event

from ..forms import InviteMemberForm, SignUpForm
from ..models import Membership, Workspace
from ..services import accept_invitation, invite_member, make_invite_token


class RemoveMemberTests(TestCase):
    """The primary admin can take someone out of the family, and their email is free again."""

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(username="Parent1", password="TestPassword123!")
        self.workspace = Workspace.objects.create(name="Reyes family", primary_admin=self.admin)
        Membership.objects.create(
            workspace=self.workspace, user=self.admin, role=Membership.Role.PARENT, invited_by=self.admin,
        )
        self.client.force_login(self.admin)

    def invite(self, email="kid@example.com", role=Membership.Role.KID, workspace=None, admin=None):
        return invite_member(
            workspace=workspace or self.workspace, invited_by=admin or self.admin, email=email, role=role,
        )

    def joined_kid(self, email="kid@example.com"):
        membership = self.invite(email)
        membership.user.set_password("KidPassword123!")
        membership.user.save()
        accept_invitation(membership)
        return membership

    def remove_url(self, membership, workspace=None):
        return reverse(
            "accounts:remove_member",
            kwargs={"workspace_id": (workspace or self.workspace).id, "membership_id": membership.pk},
        )

    def members_url(self):
        return reverse("accounts:manage_members", kwargs={"workspace_id": self.workspace.id})

    def test_removing_a_kid_who_made_plans_and_chatted_frees_their_email(self):
        kid = self.joined_kid()
        start = timezone.now() + timedelta(days=1)
        event = Event.objects.create(
            title="Soccer", starts_at=start, ends_at=start + timedelta(hours=1),
            timezone_name="America/New_York", workspace=self.workspace, created_by=kid.user,
        )
        checklist = Checklist.objects.create(title="Soccer bag", workspace=self.workspace, created_by=kid.user)
        ChatMessage.objects.create(workspace=self.workspace, sender=kid.user, content="Hi")

        response = self.client.post(self.remove_url(kid), follow=True)

        self.assertContains(response, "is no longer in Reyes family")
        self.assertFalse(get_user_model().objects.filter(email__iexact="kid@example.com").exists())
        self.assertFalse(Membership.objects.filter(pk=kid.pk).exists())
        event.refresh_from_db()
        checklist.refresh_from_db()
        self.assertEqual(event.created_by, self.admin)
        self.assertEqual(checklist.created_by, self.admin)
        self.assertFalse(ChatMessage.objects.filter(content="Hi").exists())

        signup = SignUpForm(data={
            "username": "mateo", "email": "KID@example.com",
            "password1": "AnotherPass123!", "password2": "AnotherPass123!",
        })
        self.assertTrue(signup.is_valid(), signup.errors)
        invite = InviteMemberForm(data={"email": "kid@example.com", "role": "kid"}, workspace=self.workspace)
        self.assertTrue(invite.is_valid(), invite.errors)

    def test_removed_member_is_signed_out_and_their_invite_link_stops_working(self):
        kid = self.joined_kid()
        token = make_invite_token(kid)
        kid_client = self.client_class()
        kid_client.force_login(kid.user)

        self.client.post(self.remove_url(kid))

        response = kid_client.get(reverse("accounts:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])
        response = kid_client.get(reverse("accounts:accept_invite", kwargs={"token": token}))
        self.assertEqual(response.status_code, 400)

    def test_removing_a_pending_invite_frees_the_email(self):
        pending = self.invite("mateo@example.com", Membership.Role.PARENT)
        response = self.client.get(self.members_url())
        self.assertContains(response, self.remove_url(pending))

        self.client.post(self.remove_url(pending))

        self.assertFalse(get_user_model().objects.filter(email="mateo@example.com").exists())
        invite = InviteMemberForm(data={"email": "mateo@example.com", "role": "parent"}, workspace=self.workspace)
        self.assertTrue(invite.is_valid(), invite.errors)

    def test_their_google_calendar_is_revoked_and_its_copied_events_go(self):
        kid = self.joined_kid()
        connection = CalendarConnection.objects.create(
            user=kid.user, workspace=self.workspace, provider=CalendarConnection.Provider.GOOGLE,
            access_token="ya29.access", refresh_token="1//refresh",
        )
        start = timezone.now() + timedelta(days=1)
        Event.objects.create(
            title="Dentist", starts_at=start, ends_at=start + timedelta(hours=1),
            timezone_name="America/New_York", workspace=self.workspace, created_by=kid.user,
            source=Event.Source.GOOGLE, calendar_connection=connection, external_id="g1",
        )

        with patch("accounts.services.google.revoke", return_value=True) as revoke:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(self.remove_url(kid))

        revoke.assert_called_once_with("1//refresh")
        self.assertFalse(CalendarConnection.objects.exists())
        self.assertFalse(Event.objects.filter(title="Dentist").exists())

    def test_a_second_remove_changes_nothing(self):
        kid = self.joined_kid()
        other = self.invite("other@example.com")
        self.client.post(self.remove_url(kid))

        response = self.client.post(self.remove_url(kid), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer in this family")
        self.assertTrue(Membership.objects.filter(pk=other.pk).exists())

    def test_only_the_primary_admin_can_remove(self):
        kid = self.joined_kid()
        parent = self.invite("parent@example.com", Membership.Role.PARENT)
        accept_invitation(parent)
        self.client.force_login(parent.user)

        response = self.client.post(self.remove_url(kid))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Membership.objects.filter(pk=kid.pk).exists())

    def test_the_admin_cannot_remove_themselves(self):
        own = Membership.objects.get(user=self.admin)

        response = self.client.post(self.remove_url(own), follow=True)

        self.assertContains(response, "can&#x27;t be removed")
        self.assertTrue(get_user_model().objects.filter(pk=self.admin.pk).exists())

    def test_a_member_of_another_family_is_untouched(self):
        User = get_user_model()
        other_admin = User.objects.create_user(username="Parent9", password="TestPassword123!")
        other_family = Workspace.objects.create(name="Other family", primary_admin=other_admin)
        stranger = self.invite("stranger@example.com", workspace=other_family, admin=other_admin)

        response = self.client.post(self.remove_url(stranger), follow=True)

        self.assertContains(response, "no longer in this family")
        self.assertTrue(Membership.objects.filter(pk=stranger.pk).exists())

    def test_remove_needs_a_post(self):
        kid = self.joined_kid()

        response = self.client.get(self.remove_url(kid))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(Membership.objects.filter(pk=kid.pk).exists())

    def test_the_members_page_shows_remove_for_everyone_but_the_admin(self):
        kid = self.joined_kid()
        own = Membership.objects.get(user=self.admin)

        response = self.client.get(self.members_url())

        self.assertContains(response, self.remove_url(kid))
        self.assertNotContains(response, self.remove_url(own))
        self.assertContains(response, "data-confirm-title=\"Remove ")
