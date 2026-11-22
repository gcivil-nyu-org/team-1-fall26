import math
import uuid

from django.conf import settings
from django.db import models

from syncmily import constraints
from syncmily.safety import CleanCharField, CleanCoordinatesMixin, CleanTextQuerySet


class Workspace(CleanCoordinatesMixin, models.Model):
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

    name = CleanCharField(max_length=100, policy="NAME")

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

    # Where trips start when someone chooses "from home". Only parents set it, and kids
    # use it without seeing the address.
    home_location = CleanCharField(
        max_length=255, blank=True, default="", db_default="", policy="PLACE"
    )
    home_latitude = models.FloatField(null=True, blank=True)
    home_longitude = models.FloatField(null=True, blank=True)
    coordinate_fields = (("home_latitude", "home_longitude"),)

    created_at = models.DateTimeField(auto_now_add=True)

    objects = CleanTextQuerySet.as_manager()

    class Meta:
        constraints = [
            constraints.clean_text("workspace", "name"),
            constraints.clean_text("workspace", "home_location"),
            *constraints.coordinates("workspace", "home_latitude", "home_longitude"),
        ]

    def __str__(self):
        return self.name

    @property
    def has_home(self):
        return all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in (self.home_latitude, self.home_longitude)
        )


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

    # A random value in the invite link, replaced on every resend, so a resend always
    # cancels the earlier link (invited_at alone has whole-second stamps). Invites made
    # before this column existed keep "", which their old links carry implicitly.
    invite_nonce = models.CharField(
        max_length=32, blank=True, default="", db_default="", editable=False
    )

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
            constraints.DatabaseCheck(
                condition=models.Q(color__regex=constraints.HEX_COLOR_PATTERN),
                name="membercolor_color_hex",
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

    # True when this Google account created the Syncmily account. A Google account
    # linked later from the profile page is dropped when the password changes, since a
    # stolen session could have linked it.
    came_with_account = models.BooleanField(default=False, db_default=False)

    # Set when a password change or reset cut this link. The row stays, so the Google
    # account cannot sign in or make a new account until the owner connects it again.
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.user} signs in with Google as {self.email}"

    @property
    def is_active(self):
        return self.revoked_at is None


class EmailAddress(models.Model):
    """Whether an account proved it owns its email address (hardening spec 3.4).

    Only form sign-ups get a row, unverified until the emailed link is used. An
    account without a row counts as verified: accounts from before this check,
    invited members (the invite link went to that address) and Google accounts
    (Google vouches for the address).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="email_address",
    )

    # The address the link was sent to; the link only confirms this one.
    email = models.EmailField(blank=True, default="")

    verified_at = models.DateTimeField(null=True, blank=True)

    # When the current link was sent. A link carries this time, so a newer link or
    # a used one (sent_at cleared) stops the older links working.
    sent_at = models.DateTimeField(null=True, blank=True)

    # When a Google sign-in that proved the address took it from this unverified
    # account. The owner sees a notice at the next sign-in.
    released_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        state = "verified" if self.verified_at else "not verified"
        return f"{self.user} email {state}"

    @property
    def is_verified(self):
        return self.verified_at is not None


class UsernameHistory(models.Model):
    """One row per username change: limits renames and keeps released names
    blocked for a while (hardening spec 3.2, rule 6)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="username_history",
    )
    old = models.CharField(max_length=150)
    new = models.CharField(max_length=150)
    changed_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.old} became {self.new}"
