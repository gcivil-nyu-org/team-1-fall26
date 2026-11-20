from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.models import Session
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from accounts.models import Workspace


def accessible_workspaces(user):
    """Workspaces whose chat the user is allowed to access."""
    if not user.is_authenticated or not user.is_active:
        return Workspace.objects.none()

    return Workspace.objects.filter(
        Q(primary_admin=user)
        | Q(
            memberships__user=user,
            memberships__accepted_at__isnull=False,
        )
    ).distinct()


def can_access_chat(user, workspace):
    """Check whether a user can access a workspace's chat."""
    if workspace is None:
        return False

    return accessible_workspaces(user).filter(pk=workspace.pk).exists()


def _session_matches(user, data):
    """True when decoded session data still signs in `user`: the same account and
    the password hash it was opened with (a password change ends it)."""
    if str(data.get(SESSION_KEY)) != str(user.pk):
        return False
    stored = data.get(HASH_SESSION_KEY) or ""
    if constant_time_compare(stored, user.get_session_auth_hash()):
        return True
    # Sessions signed with a retired SECRET_KEY stay valid, as in django.contrib.auth.
    return any(
        constant_time_compare(stored, user._get_session_auth_hash(secret=secret))
        for secret in getattr(settings, "SECRET_KEY_FALLBACKS", ())
    )


def authorized_sockets(workspace_id, sockets):
    """The channel names among `sockets` whose owner may still read the family chat.

    A socket passes when its user is active, is the primary admin or an accepted
    member of the family, and the session that opened it still exists, has not
    expired and still matches the user's password. This is the exact check for every
    receiver (spec 10.1, chat authorization) in two queries, however many sockets are
    open. Returns (channel names, {user id: fresh user}).
    """
    user_ids = {socket.user_id for socket in sockets}
    if not user_ids:
        return set(), {}
    users = {
        user.pk: user
        for user in get_user_model()
        .objects.filter(pk__in=user_ids, is_active=True)
        .filter(
            Q(created_workspaces__pk=workspace_id)
            | Q(
                membership__workspace_id=workspace_id,
                membership__accepted_at__isnull=False,
            )
        )
        .distinct()
    }
    keys = {
        socket.session_key
        for socket in sockets
        if socket.user_id in users and socket.session_key
    }
    sessions = {}
    if keys:
        for session in Session.objects.filter(
            session_key__in=keys, expire_date__gt=timezone.now()
        ):
            try:
                sessions[session.session_key] = session.get_decoded()
            except Exception:  # noqa: BLE001  (a damaged session signs nobody in)
                continue
    allowed = set()
    for socket in sockets:
        user = users.get(socket.user_id)
        if user is None:
            continue
        if socket.session_key is not None:
            data = sessions.get(socket.session_key)
            if data is None or not _session_matches(user, data):
                continue
        allowed.add(socket.channel_name)
    return allowed, users
