import logging
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.models import Workspace

from . import google
from .models import CalendarConnection

logger = logging.getLogger(__name__)

SESSION_KEY = "google_calendar_oauth"


def _callback_uri(request):
    return settings.GOOGLE_REDIRECT_URI or request.build_absolute_uri(
        reverse("calendars:google_callback")
    )


@login_required(login_url="admin:login")
@require_POST
def google_connect(request, workspace_id):
    workspace = get_object_or_404(Workspace, pk=workspace_id, primary_admin=request.user)
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        messages.error(request, "Google Calendar isn’t set up on this server yet.")
        return redirect("accounts:workspace_detail", workspace_id=workspace.pk)

    url, state, code_verifier = google.authorization_url(_callback_uri(request))
    request.session[SESSION_KEY] = {
        "state": state,
        "code_verifier": code_verifier,
        "workspace_id": str(workspace.pk),
    }
    return redirect(url)


@login_required(login_url="admin:login")
@require_GET
def google_callback(request):
    pending = request.session.pop(SESSION_KEY, None)
    if not pending or request.GET.get("state") != pending["state"]:
        messages.error(request, "We couldn’t confirm that request from Google. Please try connecting again.")
        return redirect("events:choose_workspace")

    workspace = get_object_or_404(Workspace, pk=pending["workspace_id"], primary_admin=request.user)
    workspace_url = reverse("accounts:workspace_detail", args=[workspace.pk])

    if request.GET.get("error") == "access_denied":
        messages.error(request, "Google Calendar wasn’t connected because access was declined.")
        return redirect(workspace_url)
    if "code" not in request.GET:
        messages.error(request, "Google couldn’t connect your calendar. Please try again.")
        return redirect(workspace_url)

    try:
        credentials, email = google.exchange_code(
            _callback_uri(request), request.GET["code"], pending["code_verifier"]
        )
    except Exception as error:
        # Log only the error type: OAuth error messages can echo request details.
        logger.warning("Google token exchange failed: %s", type(error).__name__)
        messages.error(request, "Google couldn’t connect your calendar. Please try again.")
        return redirect(workspace_url)

    granted_scopes = list(credentials.granted_scopes or [])
    if google.CALENDAR_SCOPE not in granted_scopes:
        messages.error(
            request,
            "Syncmily needs permission to see your calendar. Connect again and allow calendar access.",
        )
        return redirect(workspace_url)
    if not credentials.refresh_token:
        messages.error(request, "Google didn’t allow ongoing access. Please try connecting again.")
        return redirect(workspace_url)

    expires_at = credentials.expiry
    if expires_at is not None and timezone.is_naive(expires_at):
        expires_at = expires_at.replace(tzinfo=datetime_timezone.utc)

    CalendarConnection.objects.update_or_create(
        user=request.user,
        provider=CalendarConnection.Provider.GOOGLE,
        defaults={
            "workspace": workspace,
            "account_email": email,
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_expires_at": expires_at,
            "scopes": " ".join(granted_scopes),
            "sync_token": "",
            "last_synced_at": None,
        },
    )
    messages.success(request, f"Google Calendar connected{f' for {email}' if email else ''}.")
    return redirect(workspace_url)


@login_required(login_url="admin:login")
@require_POST
def google_disconnect(request):
    connection = get_object_or_404(
        CalendarConnection, user=request.user, provider=CalendarConnection.Provider.GOOGLE
    )
    workspace_id = connection.workspace_id
    revoked = google.revoke(connection.refresh_token or connection.access_token)
    connection.delete()

    if revoked:
        messages.success(request, "Google Calendar disconnected.")
    else:
        messages.warning(
            request,
            "Google Calendar disconnected from Syncmily, but Google didn’t confirm it. "
            "You can also remove Syncmily at myaccount.google.com/permissions.",
        )
    if workspace_id:
        return redirect("accounts:workspace_detail", workspace_id=workspace_id)
    return redirect("events:choose_workspace")
