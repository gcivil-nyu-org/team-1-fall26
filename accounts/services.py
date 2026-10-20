from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import Membership, Workspace


# Temporary until members can pick their own password at sign-up.
DEFAULT_MEMBER_PASSWORD = "123456"

INVITE_SALT = "accounts.invite"
INVITE_MAX_AGE = 7 * 24 * 60 * 60

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
        for username in get_user_model().objects.filter(
            username__istartswith=prefix,
        ).values_list("username", flat=True)
    }

    while f"{prefix}{number}".lower() in taken:
        number += 1

    return f"{prefix}{number}"


def invite_member(*, workspace, invited_by, email, role, attempts=3):
    """
    Create an inactive account for a new member. The account becomes
    usable once the invite link in their email is confirmed.
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
                user.set_password(DEFAULT_MEMBER_PASSWORD)
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


def make_invite_token(membership):
    return signing.dumps({"membership": membership.pk}, salt=INVITE_SALT)


def get_membership_from_token(token):
    """
    Return the membership for an invite token, or None if the token is
    invalid or expired.
    """
    try:
        data = signing.loads(token, salt=INVITE_SALT, max_age=INVITE_MAX_AGE)
        membership_id = int(data["membership"])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None

    return (
        Membership.objects
        .select_related("user", "workspace")
        .filter(pk=membership_id)
        .first()
    )


def send_invitation_email(request, membership):
    accept_url = request.build_absolute_uri(
        reverse(
            "accounts:accept_invite",
            kwargs={"token": make_invite_token(membership)},
        )
    )
    context = {
        "membership": membership,
        "accept_url": accept_url,
        "password": DEFAULT_MEMBER_PASSWORD,
    }

    send_mail(
        subject=f"You're invited to {membership.workspace.name} on Syncmily",
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
        Membership.objects
        .select_related("workspace")
        .filter(user=user)
        .first()
    )

    if membership:
        return {
            "workspace": membership.workspace,
            "role": membership.role,
            "is_primary_admin": False,
        }

    workspace = (
        Workspace.objects
        .filter(primary_admin=user)
        .order_by("created_at")
        .first()
    )

    if workspace:
        return {
            "workspace": workspace,
            "role": Membership.Role.PARENT,
            "is_primary_admin": True,
        }

    return None
