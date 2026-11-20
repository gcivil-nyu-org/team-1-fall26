import hashlib
import logging
import secrets
import unicodedata
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core import signing
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.mail import EmailMessage, send_mail
from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from calendars import google
from chat.registry import revoke_user_sockets
from checklists.models import Checklist
from events.models import Event

from syncmily import limits, ratelimit
from syncmily.safety import (
    enforce_quota,
    username_from_email,
    username_validators,
    is_reserved_username,
)

from .models import EmailAddress, GoogleIdentity, Membership, UsernameHistory, Workspace

logger = logging.getLogger(__name__)


# Temporary until members can pick their own password at sign-up.
INVITE_SALT = "accounts.invite"
INVITE_MAX_AGE = 7 * 24 * 60 * 60
# Invites and resends a primary admin can send, so the form can't be used to mail
# strangers in bulk (hardening spec 3.3 and 4.4). Accounts under a day old get fewer,
# and one address gets at most one invite a day from all families together.
INVITES_PER_HOUR = limits.RATES["invite_hour"].limit
INVITES_PER_DAY = limits.RATES["invite_day"].limit
INVITES_PER_DAY_NEW_ACCOUNT = limits.RATES["invite_new_account"].limit
NEW_ACCOUNT_AGE = timedelta(hours=24)
# Pending invites, and the placeholder accounts behind them, go after this long.
PENDING_INVITE_LIFETIME = timedelta(days=14)

# Email confirmation links (spec 3.4).
VERIFY_EMAIL_SALT = "accounts.verify-email"
VERIFY_EMAIL_MAX_AGE = 24 * 60 * 60

# Sign-in failures for one username from any address before password sign-in pauses
# for that account (finding bruteforce-per-username-ip). Axes still locks each
# (username, IP) pair after 5.
ACCOUNT_FAILURES_PER_HOUR = 20
ACCOUNT_LOCK_SECONDS = 15 * 60

# Renames (spec 3.2 rule 6): a name someone gave up stays blocked this long.
RELEASED_USERNAME_HOLD = timedelta(days=30)

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


def new_invite_nonce():
    return secrets.token_urlsafe(16)[:22]


def invite_member(*, workspace, invited_by, email, role, attempts=3):
    """
    Create an inactive account for a new member. It has no password until
    the member opens the invite link and chooses one.

    The members page checks the invite limits and the family size first
    (invite_refusal, create_invite); this only creates the rows.

    One address belongs to one account. The invite form refuses an address another
    account holds; if one gets here anyway (two invites at once), the invite is made
    without an email and the admin shares the link (findings AUTH-07).
    """
    if workspace.primary_admin_id != invited_by.pk:
        raise PermissionError("Only the primary admin can invite members.")

    User = get_user_model()

    for attempt in range(attempts):
        try:
            with transaction.atomic():
                user = User(
                    username=next_member_username(role),
                    email="" if email and email_is_held(email) else email,
                    is_active=False,
                )
                user.set_unusable_password()
                user.save()

                membership = Membership.objects.create(
                    workspace=workspace,
                    user=user,
                    role=role,
                    invited_by=invited_by,
                    invite_nonce=new_invite_nonce(),
                )
            record_invite_send(invited_by)
            return membership
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
        tokens = []
        for connection in user.calendar_connections.defer(
            "access_token", "refresh_token"
        ):
            try:
                tokens.append(connection.refresh_token or connection.access_token)
            except ImproperlyConfigured:  # a token we can't read can't be revoked
                continue
        Event.objects.filter(created_by=user, source=Event.Source.GOOGLE).delete()
        # A plan only this person was part of would read as a whole-family plan once
        # they are gone. Mark it, so kids never see it (parents still do).
        only_this_person = (
            Event.objects.filter(
                pk__in=Event.participants.through.objects.filter(user=user).values(
                    "event_id"
                )
            )
            .annotate(people=Count("participants"))
            .filter(people=1)
            .values("pk")
        )
        Event.objects.filter(pk__in=only_this_person).update(
            private_after_removal=True, version=F("version") + 1
        )
        Event.objects.filter(created_by=user).update(
            created_by_id=admin_id, version=F("version") + 1
        )
        Checklist.objects.filter(created_by=user).update(
            created_by_id=admin_id, version=F("version") + 1
        )
        Membership.objects.filter(invited_by=user).update(invited_by_id=admin_id)
        name = (user.first_name or "").strip() or user.get_username()
        removed_id = user.pk
        user.delete()

        # Revoke at Google only once the account is really gone.
        for token in filter(None, tokens):
            transaction.on_commit(lambda token=token: google.revoke(token))
        # Close their open chat sockets with 4403 once the removal is final.
        transaction.on_commit(lambda: revoke_user_sockets(removed_id))

    logger.info(
        "Member %s removed from workspace %s by %s",
        removed_id,
        workspace.pk,
        removed_by.pk,
    )
    return name


def make_invite_token(membership):
    # "sent" and "nonce" tie the link to the latest invite: a resend moves both, so
    # every earlier link stops working, even a resend inside the same second.
    return signing.dumps(
        {
            "membership": membership.pk,
            "sent": invite_stamp(membership),
            "nonce": membership.invite_nonce,
        },
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


# --- Invite limits (spec 3.3, 4.4) ---------------------------------------------------

INVITES_HOUR_MESSAGE = (
    "You've sent a lot of invites in the last hour. Try again a little later."
)
INVITES_DAY_MESSAGE = "You've sent a lot of invites today. Try again tomorrow."
INVITES_NEW_ACCOUNT_MESSAGE = (
    "You've sent a lot of invites for a new account. New accounts can send "
    f"{INVITES_PER_DAY_NEW_ACCOUNT} a day, so try again tomorrow."
)
INVITES_RECIPIENT_MESSAGE = (
    "That address already got an invite today, and a lot of invites to one address "
    "look like spam. Copy {username}'s invite link below and send it yourself."
)
UNVERIFIED_INVITER_MESSAGE = (
    "Confirm your email address before you invite anyone. Use the link we emailed "
    "you, or send a new one from your profile."
)
ADDRESS_TAKEN_MESSAGE = (
    "This address can't be invited. If they already use Syncmily, ask them to "
    "share their username with you, or invite them with another address."
)


class InviteRecipientLimited(Exception):
    """This address already got an invite in the last 24 hours."""


def _user_ident(user):
    return f"user:{user.pk}"


def email_ident(email):
    """Counter key for an address; a hash, so no address sits in the counter table."""
    digest = hashlib.sha256(email.strip().lower().encode()).hexdigest()
    return f"email:{digest}"


def _sent(user, scope):
    rate = ratelimit.RATE_LIMITS[scope]
    return ratelimit.get_count(scope, _user_ident(user), rate.window)


def record_invite_send(user):
    """Count one invite email against the caller's caps. Resends count too, so one
    invite can't be used to mail the same address in a loop. Invite limits are part
    of the product (they stop the form relaying mail), so they count even when
    RATELIMIT_ENABLED is off.
    """
    for scope in ("invite_hour", "invite_day", "invite_new_account"):
        ratelimit.record(scope, _user_ident(user), ratelimit.RATE_LIMITS[scope].window)


def is_new_account(user):
    return timezone.now() - user.date_joined < NEW_ACCOUNT_AGE


def invite_refusal(user):
    """Why this admin can't send another invite right now, or None.

    Checks the hourly cap, then the daily cap, then the smaller cap for accounts
    under a day old. The per-address cap applies when a mail is sent
    (send_invitation_email). Every message says "a lot of invites".
    """
    now = timezone.now()
    rows = Membership.objects.filter(invited_by=user)
    hour = max(
        rows.filter(invited_at__gte=now - timedelta(hours=1)).count(),
        _sent(user, "invite_hour"),
    )
    if hour >= INVITES_PER_HOUR:
        return INVITES_HOUR_MESSAGE
    day = max(
        rows.filter(invited_at__gte=now - timedelta(days=1)).count(),
        _sent(user, "invite_day"),
    )
    if day >= INVITES_PER_DAY:
        return INVITES_DAY_MESSAGE
    if is_new_account(user) and (
        _sent(user, "invite_new_account") >= INVITES_PER_DAY_NEW_ACCOUNT
    ):
        return INVITES_NEW_ACCOUNT_MESSAGE
    return None


def invites_left(user):
    """Kept for callers that only need a yes or no."""
    return 0 if invite_refusal(user) else 1


def email_is_held(email, *, exclude_user=None):
    """True when any other account holds this address (any case)."""
    holders = get_user_model().objects.filter(email__iexact=email.strip())
    if exclude_user is not None:
        holders = holders.exclude(pk=exclude_user.pk)
    return holders.exists()


def create_invite(*, workspace, invited_by, email, role):
    """The members page's invite: checks the family size (12 people, pending invites
    included) under the family's row lock, then creates the invite. Raises
    QuotaExceeded when the family is full."""
    with transaction.atomic():
        enforce_quota(workspace, "members")
        return invite_member(
            workspace=workspace, invited_by=invited_by, email=email, role=role
        )


def reissue_invite(membership):
    """Start a fresh invite for a pending member; older links stop working."""
    Membership.objects.filter(pk=membership.pk).update(
        invited_at=timezone.now(), invite_nonce=new_invite_nonce()
    )
    record_invite_send(membership.invited_by)
    membership.refresh_from_db(fields=["invited_at", "invite_nonce"])
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
        nonce = str(data.get("nonce", ""))
    except (signing.BadSignature, KeyError, TypeError, ValueError, AttributeError):
        return None

    membership = (
        Membership.objects.select_related("user", "workspace", "invited_by")
        .filter(pk=membership_id)
        .first()
    )
    if (
        membership is None
        or invite_stamp(membership) != sent
        or nonce != membership.invite_nonce
    ):
        return None
    return membership


def send_invitation_email(request, membership):
    """
    Email the invite link. Raises OSError (including SMTPException) when the
    mail server refuses it, so callers only say "sent" after it was accepted, and
    InviteRecipientLimited when this address already got an invite in the last day.

    The subject and body are fixed text: no family name and no inviter name, so
    nobody can put their own words or links in a Syncmily email (spec 3.3, 9.13).
    The accept page shows the family and who invited them.
    """
    email = membership.user.email
    ident = email_ident(email)
    rate = ratelimit.RATE_LIMITS["invite_recipient"]
    if ratelimit.get_count("invite_recipient", ident, rate.window) >= rate.limit:
        raise InviteRecipientLimited(email)
    context = {
        "membership": membership,
        "accept_url": invite_url(request, membership),
    }
    send_mail(
        subject="You have been invited to a family on Syncmily",
        message=render_to_string("accounts/emails/invite.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
    )
    ratelimit.record("invite_recipient", ident, rate.window)


class InviteAlreadyUsed(Exception):
    """Someone confirmed this invite first."""


def accept_invitation(membership, set_password=None):
    """Activate the invited account.

    The membership row is locked and checked again, so two posts of one link can't
    both win (finding invite-accept-race). set_password (the accept form's save)
    runs under the same lock. Raises InviteAlreadyUsed when the invite was taken.
    """
    with transaction.atomic():
        locked = Membership.objects.select_for_update().get(pk=membership.pk)
        if not locked.is_pending:
            raise InviteAlreadyUsed(membership.pk)
        if set_password is not None:
            set_password()
        membership.user.is_active = True
        membership.user.save(update_fields=["is_active"])
        locked.accepted_at = timezone.now()
        locked.save(update_fields=["accepted_at"])
        membership.accepted_at = locked.accepted_at


def _placeholder_can_go(user):
    """True when an invite's placeholder account has never been used (spec 10.1)."""
    from calendars.models import CalendarConnection
    from chat.models import ChatMessage

    return (
        not user.is_active
        and not user.has_usable_password()
        and user.last_login is None
        and not Membership.objects.filter(user=user).exists()
        and not Membership.objects.filter(invited_by=user).exists()
        and not Workspace.objects.filter(primary_admin=user).exists()
        and not GoogleIdentity.objects.filter(user=user).exists()
        and not Event.objects.filter(created_by=user).exists()
        and not Checklist.objects.filter(created_by=user).exists()
        and not ChatMessage.objects.filter(sender=user).exists()
        and not CalendarConnection.objects.filter(user=user).exists()
    )


def purge_stale_invites(now=None):
    """Delete pending invites older than 14 days (spec 4.2, 10.1). A placeholder
    account goes too, but only when one locked transaction shows it was never used.
    Returns (invites removed, accounts removed). The nightly job calls this."""
    now = now or timezone.now()
    cutoff = now - PENDING_INVITE_LIFETIME
    User = get_user_model()
    invites = accounts = 0
    stale = Membership.objects.filter(accepted_at__isnull=True, invited_at__lt=cutoff)
    for pk in list(stale.values_list("pk", flat=True)):
        with transaction.atomic():
            membership = stale.select_for_update().filter(pk=pk).first()
            if membership is None:
                continue
            user = User.objects.select_for_update().get(pk=membership.user_id)
            membership.delete()
            invites += 1
            if _placeholder_can_go(user):
                user.delete()
                accounts += 1
    return invites, accounts


def get_family_context(user):
    """
    Work out which workspace a user belongs to and their role in it.
    Returns None if the user has no workspace yet.

    Only a confirmed membership counts, like every other family gate
    (finding dashboard-ignores-accepted-at).
    """
    membership = (
        Membership.objects.select_related("workspace")
        .filter(user=user, accepted_at__isnull=False)
        .first()
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


# --- Usernames (spec 3.2) ------------------------------------------------------------


def family_usernames(user):
    """The other usernames in this person's family (members, pending invites and the
    primary admin), for the look-alike check."""
    if user is None or user.pk is None:
        return []
    workspace_ids = set(
        Membership.objects.filter(user=user).values_list("workspace_id", flat=True)
    ) | set(Workspace.objects.filter(primary_admin=user).values_list("pk", flat=True))
    if not workspace_ids:
        return []
    names = set(
        Membership.objects.filter(workspace_id__in=workspace_ids).values_list(
            "user__username", flat=True
        )
    ) | set(
        Workspace.objects.filter(pk__in=workspace_ids).values_list(
            "primary_admin__username", flat=True
        )
    )
    names.discard(user.username)
    return sorted(names)


USERNAME_TAKEN_MESSAGE = "That username is already taken."


def check_username(value, *, user=None, chosen=True):
    """Every rule for a username someone picks (spec 3.2): the text rules, then
    global case-insensitive uniqueness, then the family look-alike check, then
    names someone gave up in the last 30 days. Returns the normalized name."""
    name = username_validators(value)  # charset and reserved names
    others = get_user_model().objects.filter(username__iexact=name)
    if user is not None and user.pk is not None:
        others = others.exclude(pk=user.pk)
    if others.exists():
        raise ValidationError(USERNAME_TAKEN_MESSAGE, code="username_taken")
    username_validators(name, chosen=chosen, family_names=family_usernames(user))
    released = UsernameHistory.objects.filter(
        old__iexact=name, changed_at__gte=timezone.now() - RELEASED_USERNAME_HOLD
    )
    if user is not None and user.pk is not None:
        released = released.exclude(user=user)
    if released.exists():
        raise ValidationError(
            "That username was in use recently. Choose a different one.",
            code="username_released",
        )
    return name


def renames_left(user):
    rate = ratelimit.RATE_LIMITS["rename"]
    used = ratelimit.get_count("rename", _user_ident(user), rate.window)
    return max(rate.limit - used, 0)


def rename_user(user, new_name):
    """Save a new username and record it. Raises IntegrityError when someone took
    the name at the same moment (the unique index on lower(username))."""
    old = (
        get_user_model()
        .objects.filter(pk=user.pk)
        .values_list("username", flat=True)[0]
    )
    with transaction.atomic():
        user.username = new_name
        user.save(update_fields=["username"])
        if old != new_name:
            UsernameHistory.objects.create(
                user=user, old=old, new=new_name, changed_at=timezone.now()
            )
    rate = ratelimit.RATE_LIMITS["rename"]
    ratelimit.hit("rename", _user_ident(user), rate.limit, rate.window)
    return user


# --- Email ownership (spec 3.4) ------------------------------------------------------


def email_verified(user):
    """True unless this account signed up with the form and has not used its link
    yet. Accounts without an EmailAddress row count as verified (see the model)."""
    row = EmailAddress.objects.filter(user=user).first()
    return row is None or row.verified_at is not None


def start_email_verification(user):
    """A new form sign-up: record the address as unverified, with a fresh link."""
    row, _ = EmailAddress.objects.update_or_create(
        user=user,
        defaults={"email": user.email, "verified_at": None, "sent_at": timezone.now()},
    )
    return row


def renew_email_verification(user):
    """Send a new link (the old ones stop working). Returns the row, or None when
    there is nothing to confirm."""
    row = EmailAddress.objects.filter(user=user, verified_at__isnull=True).first()
    if row is None or not user.email:
        return None
    row.email = user.email
    row.sent_at = timezone.now()
    row.save(update_fields=["email", "sent_at"])
    return row


def email_verification_token(row):
    return signing.dumps(
        {"user": row.user_id, "sent": row.sent_at.isoformat()}, salt=VERIFY_EMAIL_SALT
    )


def email_verification_url(request, row):
    return request.build_absolute_uri(
        reverse(
            "accounts:verify_email", kwargs={"token": email_verification_token(row)}
        )
    )


def verification_message(request, row):
    body = render_to_string(
        "accounts/emails/verify_email.txt",
        {"user": row.user, "verify_url": email_verification_url(request, row)},
    )
    return EmailMessage(
        "Confirm your email address for Syncmily",
        body,
        settings.DEFAULT_FROM_EMAIL,
        [row.email],
    )


def email_row_for_token(token):
    """The unverified row a confirmation link belongs to, or None when the link is
    broken, older than 24 hours, already used or replaced by a newer one."""
    try:
        data = signing.loads(
            token, salt=VERIFY_EMAIL_SALT, max_age=VERIFY_EMAIL_MAX_AGE
        )
        user_id = int(data["user"])
        sent = str(data["sent"])
    except (signing.BadSignature, KeyError, TypeError, ValueError, AttributeError):
        return None
    row = (
        EmailAddress.objects.select_related("user")
        .filter(user_id=user_id, verified_at__isnull=True, sent_at__isnull=False)
        .first()
    )
    if (
        row is None
        or row.sent_at.isoformat() != sent
        or row.email.lower() != row.user.email.lower()
    ):
        return None
    return row


def confirm_email(token):
    """Use a confirmation link once. Returns the user, or None."""
    row = email_row_for_token(token)
    if row is None:
        return None
    with transaction.atomic():
        locked = (
            EmailAddress.objects.select_for_update()
            .filter(pk=row.pk, verified_at__isnull=True, sent_at=row.sent_at)
            .first()
        )
        if locked is None:
            return None
        locked.verified_at = timezone.now()
        locked.sent_at = None
        locked.save(update_fields=["verified_at", "sent_at"])
    return row.user


def purge_email_confirmations(now=None):
    """Nightly clean-up of confirmation records (spec 4.5). Returns
    (expired links cleared, used records removed).

    An unverified row whose link is older than 24 hours forgets the link time, so
    the owner asks for a new link. A verified row with no notice waiting is removed:
    an account without a row counts as verified, so nothing changes for its owner.
    """
    now = now or timezone.now()
    expired = EmailAddress.objects.filter(
        verified_at__isnull=True,
        sent_at__lt=now - timedelta(seconds=VERIFY_EMAIL_MAX_AGE),
    ).update(sent_at=None)
    used, _ = EmailAddress.objects.filter(
        verified_at__isnull=False, released_at__isnull=True
    ).delete()
    return expired, used


def _holds_proven_address(user):
    """Whether this account's claim on its address blocks someone else's proof.

    An unverified sign-up and an unused invite placeholder do not: neither has shown
    it owns the address (spec 3.4, findings AUTH-05 and AUTH-07)."""
    if not email_verified(user):
        return False
    is_placeholder = (
        not user.is_active
        and not user.has_usable_password()
        and user.last_login is None
    )
    return not is_placeholder


def release_address(user):
    """Take the email off an account that never proved it. Its owner sees a notice
    at the next sign-in when it was a sign-up (spec 3.4)."""
    get_user_model().objects.filter(pk=user.pk).update(email="")
    EmailAddress.objects.filter(user=user).update(
        email="", sent_at=None, released_at=timezone.now()
    )


def take_released_notice(user):
    """True once after a Google sign-in took this account's unproven address."""
    return bool(
        EmailAddress.objects.filter(user=user, released_at__isnull=False).update(
            released_at=None
        )
    )


# --- Google sign-in ------------------------------------------------------------------


class GoogleEmailInUse(Exception):
    """Another Syncmily account already uses this Google account's email."""


class GoogleLinkRevoked(Exception):
    """This Google account's link was cut when the password changed."""


def available_username(email):
    """A free, valid username from the start of an email address, e.g. maya or maya2.

    The base follows the username rules (spec 10.1). A reserved base such as "admin"
    or "parent2" gets "_" and a number, since "parent22" would still be reserved.
    """
    base = username_from_email(email)
    reserved = is_reserved_username(base)
    taken = {
        username.lower()
        for username in (
            get_user_model()
            .objects.filter(username__istartswith=base)
            .values_list("username", flat=True)
        )
    }

    def candidates():
        if not reserved:
            yield base
        separator = "_" if reserved else ""
        number = 2
        while True:
            yield f"{base}{separator}{number}"
            number += 1

    for candidate in candidates():
        if candidate.lower() not in taken and not is_reserved_username(candidate):
            return candidate


def user_for_google_account(subject, email):
    """
    Return the account linked to this Google account, creating one on first
    sign-in. Never attaches Google to an existing password account with the
    same email: whoever created that account may not own the address.

    The caller has checked that Google verified the address. That proof beats a
    sign-up that never confirmed the address and an unused invite: those accounts
    lose the address and the Google account gets it (spec 3.4).
    """
    identity = (
        GoogleIdentity.objects.select_related("user").filter(subject=subject).first()
    )
    if identity:
        if identity.revoked_at is not None:
            raise GoogleLinkRevoked(email)
        return identity.user

    User = get_user_model()
    address = email.strip().lower()
    with transaction.atomic():
        holders = list(User.objects.select_for_update().filter(email__iexact=address))
        if any(_holds_proven_address(holder) for holder in holders):
            raise GoogleEmailInUse(email)
        for holder in holders:
            release_address(holder)
        # The username keeps the letter case the person uses (Sam.Lee).
        user = User(username=available_username(email), email=address)
        user.set_unusable_password()
        user.save()
        GoogleIdentity.objects.create(
            user=user, subject=subject, email=address, came_with_account=True
        )
    return user


class GoogleAccountTaken(Exception):
    """This Google account already signs in to a different Syncmily account."""


def connect_google_account(user, subject, email):
    """Let a signed-in user sign in with this Google account from now on."""
    other = GoogleIdentity.objects.filter(subject=subject).exclude(user=user).exists()
    if other:
        raise GoogleAccountTaken(email)
    current = GoogleIdentity.objects.filter(user=user).first()
    kept = bool(current and current.subject == subject and current.came_with_account)
    GoogleIdentity.objects.update_or_create(
        user=user,
        defaults={
            "subject": subject,
            "email": email.lower(),
            "revoked_at": None,
            "came_with_account": kept,
        },
    )


# --- Passwords and sessions ----------------------------------------------------------


def end_user_sessions(user, keep_session_key=None):
    """Sign this user out everywhere except the given session."""
    stale = [
        session.session_key
        for session in Session.objects.filter(expire_date__gt=timezone.now()).exclude(
            session_key=keep_session_key
        )
        if str(session.get_decoded().get("_auth_user_id")) == str(user.pk)
    ]
    Session.objects.filter(session_key__in=stale).delete()


def revoke_google_links(user, *, linked_later_only):
    query = GoogleIdentity.objects.filter(user=user, revoked_at__isnull=True)
    if linked_later_only:
        query = query.filter(came_with_account=False)
    return query.update(revoked_at=timezone.now())


def password_changed(user, *, had_password, keep_session_key=None):
    """A password changed on the profile page. When it replaced a real password, a
    Google account linked after sign-up stops working (a stolen session could have
    linked it), and other sessions end. A first password on a Google-only account
    keeps its link. Returns how many links were cut."""
    revoked = revoke_google_links(user, linked_later_only=True) if had_password else 0
    end_user_sessions(user, keep_session_key)
    return revoked


def password_reset_completed(user, *, had_password):
    """The emailed reset link proves the address (finding AUTH-05). Confirm it; when
    the account had never confirmed it, drop every Google link and two-step device
    added before this proof. Other sessions end either way."""
    from django_otp.plugins.otp_static.models import StaticDevice
    from django_otp.plugins.otp_totp.models import TOTPDevice

    row = EmailAddress.objects.filter(user=user).first()
    if row is not None and row.verified_at is None:
        revoked = revoke_google_links(user, linked_later_only=False)
        TOTPDevice.objects.filter(user=user).delete()
        StaticDevice.objects.filter(user=user).delete()
        EmailAddress.objects.filter(pk=row.pk).update(
            email=user.email, verified_at=timezone.now(), sent_at=None
        )
    else:
        revoked = (
            revoke_google_links(user, linked_later_only=True) if had_password else 0
        )
    end_user_sessions(user)
    return revoked


# --- Sign-in throttle per account ----------------------------------------------------


def _sign_in_ident(username):
    name = unicodedata.normalize("NFKC", str(username or ""))[:150].casefold()
    return f"username:{name}"


def note_failed_sign_in(username):
    """Count a wrong password for this username from any address. At 20 in an hour,
    password sign-in pauses for this account for 15 minutes (reset stays open).
    This holds even when RATELIMIT_ENABLED is off, like the axes lockout."""
    ident = _sign_in_ident(username)
    count = ratelimit.record(
        "login_account", ident, ratelimit.RATE_LIMITS["login_account"].window
    )
    if count >= ACCOUNT_FAILURES_PER_HOUR:
        ratelimit.set_count("login_account_lock", ident, ACCOUNT_LOCK_SECONDS, 1)


def sign_in_paused(username):
    return (
        ratelimit.get_count(
            "login_account_lock", _sign_in_ident(username), ACCOUNT_LOCK_SECONDS
        )
        > 0
    )


# --- Neutral sign-up (spec 10.2) -----------------------------------------------------


def signup_conflict(username, email):
    """Why a sign-up can't create an account, without telling the visitor:
    ("account", holder) when the address belongs to an account, ("invite",
    membership) when a pending invite holds it, ("username", None) when only the
    username is taken, or (None, None)."""
    User = get_user_model()
    holder = User.objects.filter(email__iexact=email).first()
    if holder is not None:
        membership = (
            Membership.objects.select_related("user")
            .filter(user=holder, accepted_at__isnull=True)
            .first()
        )
        if membership is not None and not holder.is_active:
            return "invite", membership
        return "account", holder
    if User.objects.filter(username__iexact=username).exists():
        return "username", None
    return None, None


def signup_notice(request, kind, email, subject_obj, username):
    """The email the address owner gets instead of a page that names the cause.
    Returns None when this address already got 3 such emails in the last hour."""
    rate = ratelimit.RATE_LIMITS["signup_notice"]
    if not ratelimit.hit("signup_notice", email_ident(email), rate.limit, rate.window):
        return None
    context = {
        "kind": kind,
        "username": username,
        "login_url": request.build_absolute_uri(reverse("accounts:login")),
        "reset_url": request.build_absolute_uri(reverse("accounts:password_reset")),
        "signup_url": request.build_absolute_uri(reverse("accounts:signup")),
    }
    if kind == "account":
        context["account_username"] = subject_obj.username
    elif kind == "invite":
        context["accept_url"] = invite_url(request, subject_obj)
    body = render_to_string("accounts/emails/signup_notice.txt", context)
    return EmailMessage(
        "About your Syncmily sign-up", body, settings.DEFAULT_FROM_EMAIL, [email]
    )
