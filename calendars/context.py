"""The Google Calendar account the account menu offers to switch, on every page."""

from accounts.models import Workspace

from .models import CalendarConnection


def google_calendar_context(user, workspace):
    """The Google account shown in the account menu, and the workspace a (re)connect
    imports into.
    """
    connection = (
        CalendarConnection.objects.filter(
            user=user, provider=CalendarConnection.Provider.GOOGLE
        )
        .select_related("workspace")
        .first()
    )
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
