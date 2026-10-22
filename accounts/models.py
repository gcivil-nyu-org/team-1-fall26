import uuid

from django.conf import settings
from django.db import models


class Workspace(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    name = models.CharField(max_length=100)

    primary_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_workspaces",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Membership(models.Model):
    class Role(models.TextChoices):
        PARENT = "parent", "Parent"
        KID = "kid", "Kid"

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="membership",
    )

    role = models.CharField(max_length=10, choices=Role.choices)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sent_invitations",
    )

    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["role", "invited_at"]

    def __str__(self):
        return f"{self.user} ({self.get_role_display()}) in {self.workspace}"

    @property
    def is_pending(self):
        return self.accepted_at is None


class MemberColor(models.Model):
    """A family member's colour on the schedule, chosen by a parent."""

    KID_DEFAULT = "#9cc9f0"
    PARENT_DEFAULT = "#c62828"

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="member_colors",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="schedule_colors",
    )

    color = models.CharField(max_length=7)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                name="one_color_per_member",
            ),
        ]

    def __str__(self):
        return f"{self.user} is {self.color} in {self.workspace}"
