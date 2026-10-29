from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Membership, Workspace
from events.models import Event

from ..models import Checklist, ChecklistItem

class ChecklistTests(TestCase):

    def setUp(self):
        User = get_user_model()

        self.parent = User.objects.create_user(
            username="testparent",
            password="TestPassword123!",
        )

        self.workspace = Workspace.objects.create(
            name="Test Family",
            primary_admin=self.parent,
        )

        self.client.force_login(self.parent)

    def test_parent_can_create_checklist(self):
        response = self.client.post(
            reverse(
                "checklists:create",
                args=[self.workspace.pk],
            ),
            {
                "title": "Weekend Grocery Shopping",
                "event": "",
            },
        )

        self.assertEqual(response.status_code, 302)

        checklist = Checklist.objects.get(
            title="Weekend Grocery Shopping",
        )

        self.assertEqual(checklist.workspace, self.workspace)
        self.assertEqual(checklist.created_by, self.parent)

    def test_parent_can_add_item(self):
        checklist = Checklist.objects.create(
            title="Weekend Grocery Shopping",
            workspace=self.workspace,
            created_by=self.parent,
        )

        response = self.client.post(
            reverse(
                "checklists:item_add",
                args=[checklist.pk],
            ),
            {"description": "Buy milk"},
        )

        self.assertEqual(response.status_code, 302)

        self.assertTrue(
            ChecklistItem.objects.filter(
                checklist=checklist,
                description="Buy milk",
            ).exists()
        )

    def test_parent_can_complete_item(self):
        checklist = Checklist.objects.create(
            title="Weekend Grocery Shopping",
            workspace=self.workspace,
            created_by=self.parent,
        )

        item = ChecklistItem.objects.create(
            checklist=checklist,
            description="Buy milk",
        )

        response = self.client.post(
            reverse(
                "checklists:item_toggle",
                args=[item.pk],
            ),
        )

        self.assertEqual(response.status_code, 302)

        item.refresh_from_db()

        self.assertTrue(item.is_completed)
        self.assertEqual(item.completed_by, self.parent)
        self.assertIsNotNone(item.completed_at)

    def test_completion_can_be_reversed(self):
        checklist = Checklist.objects.create(
            title="Weekend Grocery Shopping",
            workspace=self.workspace,
            created_by=self.parent,
        )

        item = ChecklistItem.objects.create(
            checklist=checklist,
            description="Buy milk",
        )

        url = reverse(
            "checklists:item_toggle",
            args=[item.pk],
        )

        self.client.post(url)
        self.client.post(url)

        item.refresh_from_db()

        self.assertFalse(item.is_completed)
        self.assertIsNone(item.completed_by)
        self.assertIsNone(item.completed_at)


    def test_parent_can_view_checklist(self):
        checklist = Checklist.objects.create(
            title="Family Packing List",
            workspace=self.workspace,
            created_by=self.parent,
        )

        response = self.client.get(
            reverse("checklists:detail", args=[checklist.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Family Packing List")

    def test_parent_can_edit_checklist(self):
        checklist = Checklist.objects.create(
            title="Old Title",
            workspace=self.workspace,
            created_by=self.parent,
        )

        response = self.client.post(
            reverse("checklists:edit", args=[checklist.pk]),
            {"title": "Updated Title", "event": ""},
        )

        self.assertEqual(response.status_code, 302)
        checklist.refresh_from_db()
        self.assertEqual(checklist.title, "Updated Title")

    def test_parent_can_edit_and_delete_items(self):
        checklist = Checklist.objects.create(
            title="Groceries",
            workspace=self.workspace,
            created_by=self.parent,
        )
        item = ChecklistItem.objects.create(
            checklist=checklist,
            description="Milk",
        )

        response = self.client.post(
            reverse("checklists:item_edit", args=[item.pk]),
            {"description": "Oat milk"},
        )

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.description, "Oat milk")

        response = self.client.post(
            reverse("checklists:item_delete", args=[item.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            ChecklistItem.objects.filter(pk=item.pk).exists()
        )

    def test_parent_can_delete_checklist(self):
        checklist = Checklist.objects.create(
            title="Old Checklist",
            workspace=self.workspace,
            created_by=self.parent,
        )

        response = self.client.post(
            reverse("checklists:delete", args=[checklist.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Checklist.objects.filter(pk=checklist.pk).exists()
        )

    def test_accepted_kid_can_view_and_complete_items(self):
        User = get_user_model()
        kid = User.objects.create_user(
            username="testkid",
            password="TestPassword123!",
        )

        Membership.objects.create(
            workspace=self.workspace,
            user=kid,
            role=Membership.Role.KID,
            invited_by=self.parent,
            accepted_at=timezone.now(),
        )

        checklist = Checklist.objects.create(
            title="School Packing",
            workspace=self.workspace,
            created_by=self.parent,
        )
        item = ChecklistItem.objects.create(
            checklist=checklist,
            description="Pack lunch",
        )

        self.client.force_login(kid)

        response = self.client.get(
            reverse("checklists:detail", args=[checklist.pk])
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse("checklists:item_toggle", args=[item.pk])
        )
        self.assertEqual(response.status_code, 302)

        item.refresh_from_db()
        self.assertTrue(item.is_completed)
        self.assertEqual(item.completed_by, kid)

    def test_kid_cannot_create_or_edit_checklists(self):
        User = get_user_model()
        kid = User.objects.create_user(
            username="restrictedkid",
            password="TestPassword123!",
        )

        Membership.objects.create(
            workspace=self.workspace,
            user=kid,
            role=Membership.Role.KID,
            invited_by=self.parent,
            accepted_at=timezone.now(),
        )

        checklist = Checklist.objects.create(
            title="Protected Checklist",
            workspace=self.workspace,
            created_by=self.parent,
        )

        self.client.force_login(kid)

        response = self.client.post(
            reverse("checklists:create", args=[self.workspace.pk]),
            {"title": "Unauthorized", "event": ""},
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.post(
            reverse("checklists:edit", args=[checklist.pk]),
            {"title": "Changed", "event": ""},
        )
        self.assertEqual(response.status_code, 404)

        checklist.refresh_from_db()
        self.assertEqual(checklist.title, "Protected Checklist")

    def test_outsider_cannot_access_checklist(self):
        User = get_user_model()
        outsider = User.objects.create_user(
            username="outsider",
            password="TestPassword123!",
        )

        checklist = Checklist.objects.create(
            title="Private Family Checklist",
            workspace=self.workspace,
            created_by=self.parent,
        )

        self.client.force_login(outsider)

        response = self.client.get(
            reverse("checklists:detail", args=[checklist.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_can_associate_checklist_with_family_event(self):
        event = Event.objects.create(
            title="Family Picnic",
            workspace=self.workspace,
            created_by=self.parent,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
        )

        response = self.client.post(
            reverse("checklists:create", args=[self.workspace.pk]),
            {"title": "Picnic Packing", "event": event.pk},
        )

        self.assertEqual(response.status_code, 302)

        checklist = Checklist.objects.get(title="Picnic Packing")
        self.assertEqual(checklist.event, event)

    def test_completion_progress_is_correct(self):
        checklist = Checklist.objects.create(
            title="Weekend Tasks",
            workspace=self.workspace,
            created_by=self.parent,
        )

        ChecklistItem.objects.create(
            checklist=checklist,
            description="Task one",
            is_completed=True,
        )
        ChecklistItem.objects.create(
            checklist=checklist,
            description="Task two",
            is_completed=False,
        )

        response = self.client.get(
            reverse("checklists:detail", args=[checklist.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["progress"], 50)
        self.assertEqual(response.context["completed_items"], 1)
        self.assertEqual(response.context["total_items"], 2)


    def test_pending_member_cannot_view_checklist(self):
        User = get_user_model()
        pending_user = User.objects.create_user(
            username="pendinguser",
            password="TestPassword123!",
        )

        Membership.objects.create(
            workspace=self.workspace,
            user=pending_user,
            role=Membership.Role.KID,
            invited_by=self.parent,
            accepted_at=None,
        )

        checklist = Checklist.objects.create(
            title="Private Family Tasks",
            workspace=self.workspace,
            created_by=self.parent,
        )

        self.client.force_login(pending_user)

        response = self.client.get(
            reverse("checklists:detail", args=[checklist.pk])
        )

        self.assertEqual(response.status_code, 404)

    def test_cannot_link_event_from_another_workspace(self):
        User = get_user_model()
        other_parent = User.objects.create_user(
            username="otherparent",
            password="TestPassword123!",
        )

        other_workspace = Workspace.objects.create(
            name="Other Family",
            primary_admin=other_parent,
        )

        other_event = Event.objects.create(
            title="Other Family Event",
            workspace=other_workspace,
            created_by=other_parent,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
        )

        response = self.client.post(
            reverse("checklists:create", args=[self.workspace.pk]),
            {
                "title": "Invalid Checklist",
                "event": other_event.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Checklist.objects.filter(
                title="Invalid Checklist"
            ).exists()
        )

    def test_kid_cannot_delete_checklist_or_items(self):
        User = get_user_model()
        kid = User.objects.create_user(
            username="securitykid",
            password="TestPassword123!",
        )

        Membership.objects.create(
            workspace=self.workspace,
            user=kid,
            role=Membership.Role.KID,
            invited_by=self.parent,
            accepted_at=timezone.now(),
        )

        checklist = Checklist.objects.create(
            title="Protected Tasks",
            workspace=self.workspace,
            created_by=self.parent,
        )

        item = ChecklistItem.objects.create(
            checklist=checklist,
            description="Important task",
        )

        self.client.force_login(kid)

        response = self.client.post(
            reverse("checklists:delete", args=[checklist.pk])
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.post(
            reverse("checklists:item_delete", args=[item.pk])
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.post(
            reverse("checklists:item_edit", args=[item.pk]),
            {"description": "Unauthorized edit"},
        )
        self.assertEqual(response.status_code, 404)

        item.refresh_from_db()
        self.assertEqual(item.description, "Important task")
        self.assertTrue(Checklist.objects.filter(pk=checklist.pk).exists())
