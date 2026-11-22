import logging
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import BadRequest, ImproperlyConfigured
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from google.auth.exceptions import RefreshError

from accounts.models import Workspace
from syncmily.ratelimit import rate_limit
from syncmily.safety import Params, safe_next_url

from . import google
from .models import CalendarConnection
from .sync import record_sync_failure, sync_connection

logger = logging.getLogger(__name__)

SESSION_KEY = "google_calendar_oauth"
# The token columns decrypt when a row loads. Views that must still work when a token
# can't be decrypted (a lost key) leave them out and read them only inside a try.
TOKEN_FIELDS = ("access_token", "refresh_token")
# What Google sends back: state and code are URL-safe strings; error is a short word.
OAUTH_VALUE = r"[A-Za-z0-9._~/+=-]+"
OAUTH_ERROR = r"[a-z_]+"


def _safe_next(request, url):
    """Return url when it points back into this site, else an empty string."""
    return safe_next_url(request, url)


def _stored_token(connection):
    """The token to revoke, or "" when it can't be decrypted."""
    try:
        return connection.refresh_token or connection.access_token
    except ImproperlyConfigured:
        return ""


def _callback_uri(request):
    return settings.GOOGLE_REDIRECT_URI or request.build_absolute_uri(
        reverse("calendars:google_callback")
    )


@login_required
@require_POST
@rate_limit("google_oauth")
def google_connect(request, workspace_id):
    workspace = get_object_or_404(
        Workspace, pk=workspace_id, primary_admin=request.user
    )
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        messages.error(request, "Google Calendar isn’t set up on this server yet.")
        return redirect("accounts:workspace_detail", workspace_id=workspace.pk)

    url, state, code_verifier = google.authorization_url(_callback_uri(request))
    request.session[SESSION_KEY] = {
        "state": state,
        "code_verifier": code_verifier,
        "workspace_id": str(workspace.pk),
    }
    next_url = Params(request.POST, request).next_url("next")
    if next_url:
        request.session[SESSION_KEY]["next"] = next_url
    return redirect(url)


def _oauth_params(request):
    """The state, code and error Google sent back; a malformed one reads as missing."""
    params = Params(request.GET, request)
    values = {}
    for name, pattern, longest in (
        ("state", OAUTH_VALUE, 512),
        ("code", OAUTH_VALUE, 2048),
        ("error", OAUTH_ERROR, 64),
    ):
        try:
            values[name] = params.token(name, pattern, longest)
        except BadRequest:
            values[name] = None
    return values


@login_required
@require_GET
@rate_limit("google_oauth", methods=("GET",))
def google_callback(request):
    pending = request.session.pop(SESSION_KEY, None)
    returned = _oauth_params(request)
    if not pending or returned["state"] != pending["state"]:
        messages.error(
            request,
            "We couldn’t confirm that request from Google. Please try connecting"
            " again.",
        )
        return redirect("events:choose_workspace")

    workspace = get_object_or_404(
        Workspace, pk=pending["workspace_id"], primary_admin=request.user
    )
    workspace_url = _safe_next(request, pending.get("next")) or reverse(
        "accounts:workspace_detail", args=[workspace.pk]
    )

    if returned["error"] == "access_denied":
        messages.error(
            request, "Google Calendar wasn’t connected because access was declined."
        )
        return redirect(workspace_url)
    if not returned["code"]:
        messages.error(
            request, "Google couldn’t connect your calendar. Please try again."
        )
        return redirect(workspace_url)

    try:
        credentials, email = google.exchange_code(
            _callback_uri(request), returned["code"], pending["code_verifier"]
        )
    except Exception as error:
        # Log only the error type: OAuth error messages can echo request details.
        # The log line carries the exception class name only, never its message.
        logger.warning(  # nosemgrep: python-logger-credential-disclosure
            "Google token exchange failed: %s", type(error).__name__
        )
        messages.error(
            request, "Google couldn’t connect your calendar. Please try again."
        )
        return redirect(workspace_url)

    granted_scopes = list(credentials.granted_scopes or [])
    if google.CALENDAR_SCOPE not in granted_scopes:
        messages.error(
            request,
            "Syncmily needs permission to see your calendar. Connect again and tick the"
            " calendar box on Google’s screen.",
        )
        return redirect(workspace_url)
    if not credentials.refresh_token:
        messages.error(
            request, "Google didn’t allow ongoing access. Please try connecting again."
        )
        return redirect(workspace_url)

    expires_at = credentials.expiry
    if expires_at is not None and timezone.is_naive(expires_at):
        expires_at = expires_at.replace(tzinfo=datetime_timezone.utc)

    previous = (
        CalendarConnection.objects.defer(*TOKEN_FIELDS)
        .filter(user=request.user, provider=CalendarConnection.Provider.GOOGLE)
        .first()
    )
    if (
        previous
        and previous.account_email
        and email
        and previous.account_email != email
    ):
        # Switching Google accounts: end the old account's access and drop its imported
        # events.
        # (Revoking the same account's grant would also cancel the new token, so only do
        # it here.)
        google.revoke(_stored_token(previous))
        previous.events.all().delete()

    connection, _ = CalendarConnection.objects.update_or_create(
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
            "sync_error": CalendarConnection.SyncError.NONE,
            "sync_failed_at": None,
        },
    )
    messages.success(
        request, f"Google Calendar connected{f' for {email}' if email else ''}."
    )
    _sync_and_report(request, connection)
    return redirect(workspace_url)


def _sync_and_report(request, connection):
    try:
        result = sync_connection(connection)
    except RefreshError as error:
        record_sync_failure(connection, error)
        messages.error(
            request,
            "Google no longer accepts Syncmily’s access. Disconnect and connect Google"
            " Calendar again.",
        )
    except Exception as error:
        record_sync_failure(connection, error)
        logger.warning("Google Calendar import failed: %s", type(error).__name__)
        messages.error(
            request,
            "We couldn’t import your Google Calendar events. Please try again later.",
        )
    else:
        messages.success(request, f"Google Calendar events imported: {result}.")


@login_required
@require_POST
@rate_limit("google_sync")
def google_sync(request):
    connection = get_object_or_404(
        CalendarConnection.objects.defer(*TOKEN_FIELDS),
        user=request.user,
        provider=CalendarConnection.Provider.GOOGLE,
    )
    _sync_and_report(request, connection)
    if connection.workspace_id:
        return redirect(
            "accounts:workspace_detail", workspace_id=connection.workspace_id
        )
    return redirect("events:choose_workspace")


@login_required
@require_POST
def google_auto_sync(request):
    connection = get_object_or_404(
        CalendarConnection.objects.defer(*TOKEN_FIELDS),
        user=request.user,
        provider=CalendarConnection.Provider.GOOGLE,
    )
    try:
        choice = Params(request.POST, request).choice("auto_sync", ("on", "off"))
    except BadRequest:
        choice = None
    if choice is None:
        messages.error(request, "Choose whether automatic sync is on or paused.")
    else:
        connection.auto_sync = choice == "on"
        connection.save(update_fields=["auto_sync", "updated_at"])
        if connection.auto_sync:
            messages.success(
                request,
                "Automatic sync is on. Google Calendar changes come in every 15"
                " minutes.",
            )
        else:
            messages.success(
                request,
                "Automatic sync is paused. Use Import events now when you want an"
                " update.",
            )
    if connection.workspace_id:
        return redirect(
            "accounts:workspace_detail", workspace_id=connection.workspace_id
        )
    return redirect("events:choose_workspace")


@login_required
@require_POST
def google_disconnect(request):
    connection = get_object_or_404(
        CalendarConnection.objects.defer(*TOKEN_FIELDS),
        user=request.user,
        provider=CalendarConnection.Provider.GOOGLE,
    )
    workspace_id = connection.workspace_id
    revoked = google.revoke(_stored_token(connection))
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
