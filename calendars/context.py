"""The Google Calendar account the account menu offers to switch, on every page."""

import logging

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from accounts.models import Workspace

from .models import CalendarConnection

logger = logging.getLogger(__name__)


def _connection(user):
    """The user's Google connection. A token that can't be decrypted (a lost or
    rotated key) must not break every page, so the row loads without its token
    columns and shows the reconnect banner."""
    rows = CalendarConnection.objects.filter(
        user=user, provider=CalendarConnection.Provider.GOOGLE
    ).select_related("workspace")
    try:
        return rows.first()
    except ImproperlyConfigured:
        logger.warning("A stored Google token can't be decrypted; asking to reconnect")
    connection = rows.defer("access_token", "refresh_token").first()
    if connection is not None:
        connection.sync_error = CalendarConnection.SyncError.ACCESS_REVOKED
        connection.sync_failed_at = connection.sync_failed_at or timezone.now()
    return connection


def google_calendar_context(user, workspace):
    """The Google account shown in the account menu, and the workspace a (re)connect
    imports into.
    """
    connection = _connection(user)
    # Keep sending events where they already go; otherwise use this page's family, then
    # any the user runs.
    if (
        connection
        and connection.workspace
        and connection.workspace.primary_admin_id == user.pk
    ):
        target = connection.workspace
    elif workspace and workspace.primary_admin_id == user.pk:
        target = workspace
    else:
        target = (
            Workspace.objects.filter(primary_admin=user).order_by("created_at").first()
        )
    return {"calendar_connection": connection, "calendar_workspace": target}
