import uuid

from django.conf import settings
from django.db import models


class Workspace(models.Model):
    class Palette(models.TextChoices):
        # Stored keys stay as they were so saved families keep their choice; the labels
        # are the
        # names parents see. Saturday market is the default.
        PRIMARIES = "primaries", "Saturday market"
        GARDEN = "garden", "Morning garden"
        BOLD = "bold", "Crayon box"

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

    # The paint deck the family's schedule is drawn in, chosen by a parent.
    palette = models.CharField(
        max_length=16,
        choices=Palette.choices,
        default=Palette.PRIMARIES,
        db_default=Palette.PRIMARIES,
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

    # A kid's own pick of the family presets, for their screens only; blank follows the
    # family's.
    palette = models.CharField(
        max_length=16,
        choices=Workspace.Palette.choices,
        blank=True,
        default="",
        db_default="",
    )

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


class BriefingSeen(models.Model):
    """The local day someone last closed their "Your day" briefing; it opens again the
    next day.

    Kept per user rather than on Membership, because a family's primary admin has no
    membership.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="briefing_seen",
    )

    seen_on = models.DateField()

    def __str__(self):
        return f"{self.user} closed their briefing on {self.seen_on}"


class GoogleIdentity(models.Model):
    """The Google account that signs in to a Syncmily account."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_identity",
    )

    # Google's permanent account ID (the "sub" claim). Emails can change; this can't.
    subject = models.CharField(max_length=255, unique=True)

    email = models.EmailField()

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} signs in with Google as {self.email}"
