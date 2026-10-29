import logging
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from calendars import google
from checklists.models import Checklist
from events.models import Event

from .models import GoogleIdentity, Membership, Workspace

logger = logging.getLogger(__name__)


# Temporary until members can pick their own password at sign-up.
INVITE_SALT = "accounts.invite"
INVITE_MAX_AGE = 7 * 24 * 60 * 60
# Invites and resends a primary admin can send per hour, so the form can't be
# used to mail strangers in bulk.
INVITES_PER_HOUR = 20

USERNAME_PREFIXES = {
    Membership.Role.PARENT: ("Parent", 2),  # Parent1 is the primary admin.
    Membership.Role.KID: ("Kid", 1),
}


def next_member_username(role):
    """
    Return the first free username for a role, e.g. Parent2 or Kid3.
    Usernames are global, so numbering continues across workspaces.
    """
    prefix, number = USERNAME_PREFIXES[role]
    taken = {
        username.lower()
        for username in (
            get_user_model()
            .objects.filter(
                username__istartswith=prefix,
            )
            .values_list("username", flat=True)
        )
    }

    while f"{prefix}{number}".lower() in taken:
        number += 1

    return f"{prefix}{number}"


def invite_member(*, workspace, invited_by, email, role, attempts=3):
    """
    Create an inactive account for a new member. It has no password until
    the member opens the invite link and chooses one.
    """
    if workspace.primary_admin_id != invited_by.pk:
        raise PermissionError("Only the primary admin can invite members.")

    User = get_user_model()

    for attempt in range(attempts):
        try:
            with transaction.atomic():
                user = User(
                    username=next_member_username(role),
                    email=email,
                    is_active=False,
                )
                user.set_unusable_password()
                user.save()

                return Membership.objects.create(
                    workspace=workspace,
                    user=user,
                    role=role,
                    invited_by=invited_by,
                )
        except IntegrityError:
            # Another invite took the same username first; pick the next one.
            if attempt == attempts - 1:
                raise


def remove_member(*, workspace, removed_by, membership_id):
    """
    Take someone out of the family for good, so their email is free for a new
    account or invite. Returns the removed user's display name, or None when
    there was nobody to remove (already gone, or not in this workspace).

    Plans and checklists they made stay with the family, handed to the primary
    admin. Events copied in from their own Google Calendar, their chat messages,
    colours and calendar connections are deleted with the account.
    """
    if workspace.primary_admin_id != removed_by.pk:
        raise PermissionError("Only the primary admin can remove members.")

    with transaction.atomic():
        membership = (
            Membership.objects.select_for_update()
            .select_related("user")
            .filter(workspace=workspace, pk=membership_id)
            .first()
        )
        if membership is None:
            return None
        user = membership.user
        if user.pk in (workspace.primary_admin_id, removed_by.pk):
            raise PermissionError("The primary admin can't be removed.")
        if Workspace.objects.filter(primary_admin=user).exists():
            raise PermissionError("This person runs another family.")

        admin_id = workspace.primary_admin_id
        tokens = [
            connection.refresh_token or connection.access_token
            for connection in user.calendar_connections.all()
        ]
        Event.objects.filter(created_by=user, source=Event.Source.GOOGLE).delete()
        Event.objects.filter(created_by=user).update(created_by_id=admin_id)
        Checklist.objects.filter(created_by=user).update(created_by_id=admin_id)
        Membership.objects.filter(invited_by=user).update(invited_by_id=admin_id)
        name = (user.first_name or "").strip() or user.get_username()
        removed_id = user.pk
        user.delete()

        # Revoke at Google only once the account is really gone.
        for token in filter(None, tokens):
            transaction.on_commit(lambda token=token: google.revoke(token))

    logger.info(
        "Member %s removed from workspace %s by %s",
        removed_id,
        workspace.pk,
        removed_by.pk,
    )
    return name


def make_invite_token(membership):
    # "sent" ties the link to the latest invite: a resend moves invited_at,
    # so every earlier link stops working.
    return signing.dumps(
        {"membership": membership.pk, "sent": invite_stamp(membership)},
        salt=INVITE_SALT,
    )


def invite_stamp(membership):
    return int(membership.invited_at.timestamp())


def invite_url(request, membership):
    return request.build_absolute_uri(
        reverse(
            "accounts:accept_invite",
            kwargs={"token": make_invite_token(membership)},
        )
    )


def invites_left(user):
    sent = Membership.objects.filter(
        invited_by=user,
        invited_at__gte=timezone.now() - timedelta(hours=1),
    ).count()
    return max(INVITES_PER_HOUR - sent, 0)


def reissue_invite(membership):
    """Start a fresh invite for a pending member; older links stop working."""
    Membership.objects.filter(pk=membership.pk).update(invited_at=timezone.now())
    membership.refresh_from_db(fields=["invited_at"])
    return membership


def get_membership_from_token(token):
    """
    Return the membership for an invite token, or None if the token is
    invalid or expired.
    """
    try:
        data = signing.loads(token, salt=INVITE_SALT, max_age=INVITE_MAX_AGE)
        membership_id = int(data["membership"])
        sent = int(data["sent"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None

    membership = (
        Membership.objects.select_related("user", "workspace")
        .filter(pk=membership_id)
        .first()
    )
    if membership is None or invite_stamp(membership) != sent:
        return None
    return membership


def send_invitation_email(request, membership):
    """
    Email the invite link. Raises OSError (including SMTPException) when the
    mail server refuses it, so callers only say "sent" after it was accepted.
    """
    context = {
        "membership": membership,
        "accept_url": invite_url(request, membership),
    }

    send_mail(
        subject=f"Join {membership.workspace.name} on Syncmily",
        message=render_to_string("accounts/emails/invite.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[membership.user.email],
    )


def accept_invitation(membership):
    with transaction.atomic():
        membership.user.is_active = True
        membership.user.save(update_fields=["is_active"])
        membership.accepted_at = timezone.now()
        membership.save(update_fields=["accepted_at"])


def get_family_context(user):
    """
    Work out which workspace a user belongs to and their role in it.
    Returns None if the user has no workspace yet.
    """
    membership = (
        Membership.objects.select_related("workspace").filter(user=user).first()
    )

    if membership:
        return {
            "workspace": membership.workspace,
            "role": membership.role,
            "is_primary_admin": False,
        }

    workspace = (
        Workspace.objects.filter(primary_admin=user).order_by("created_at").first()
    )

    if workspace:
        return {
            "workspace": workspace,
            "role": Membership.Role.PARENT,
            "is_primary_admin": True,
        }

    return None


class GoogleEmailInUse(Exception):
    """Another Syncmily account already uses this Google account's email."""


def available_username(email):
    """A free username based on the start of an email address, e.g. maya or maya2."""
    base = re.sub(r"[^\w.+-]", "", email.split("@")[0])[:140] or "member"
    taken = {
        username.lower()
        for username in (
            get_user_model()
            .objects.filter(
                username__istartswith=base,
            )
            .values_list("username", flat=True)
        )
    }
    candidate, number = base, 1
    while candidate.lower() in taken:
        number += 1
        candidate = f"{base}{number}"
    return candidate


def user_for_google_account(subject, email):
    """
    Return the account linked to this Google account, creating one on first
    sign-in. Never attaches Google to an existing password account with the
    same email: whoever created that account may not own the address.
    """
    identity = (
        GoogleIdentity.objects.select_related("user").filter(subject=subject).first()
    )
    if identity:
        return identity.user

    User = get_user_model()
    if User.objects.filter(email__iexact=email).exists():
        raise GoogleEmailInUse(email)

    with transaction.atomic():
        user = User(username=available_username(email), email=email.lower())
        user.set_unusable_password()
        user.save()
        GoogleIdentity.objects.create(user=user, subject=subject, email=email.lower())
    return user


class GoogleAccountTaken(Exception):
    """This Google account already signs in to a different Syncmily account."""


def connect_google_account(user, subject, email):
    """Let a signed-in user sign in with this Google account from now on."""
    other = GoogleIdentity.objects.filter(subject=subject).exclude(user=user).exists()
    if other:
        raise GoogleAccountTaken(email)
    GoogleIdentity.objects.update_or_create(
        user=user,
        defaults={"subject": subject, "email": email.lower()},
    )
