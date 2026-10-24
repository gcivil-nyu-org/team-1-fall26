from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from events.models import Event

# Import a rolling window: recent past events plus the coming months.
PAST_DAYS = 30
FUTURE_DAYS = 180


class SyncResult:
    def __init__(self):
        self.created = self.updated = self.deleted = self.skipped = 0

    def __str__(self):
        return (
            f"{self.created} added, {self.updated} updated, "
            f"{self.deleted} removed, {self.skipped} skipped"
        )


def _credentials(connection):
    expiry = connection.token_expires_at
    return Credentials(
        token=connection.access_token or None,
        # google-auth compares expiry as naive UTC and refreshes the token when due.
        expiry=expiry.astimezone(datetime_timezone.utc).replace(tzinfo=None) if expiry else None,
        refresh_token=connection.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        scopes=connection.scopes.split(),
    )


def _service(credentials):
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def _zone(name, fallback):
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(fallback), fallback


def _event_times(item, calendar_timezone):
    """Return (starts_at, ends_at, timezone_name) for a Google event, or None."""
    start, end = item.get("start", {}), item.get("end", {})
    zone, zone_name = _zone(start.get("timeZone") or calendar_timezone, settings.TIME_ZONE)
    if "dateTime" in start and "dateTime" in end:
        starts_at = datetime.fromisoformat(start["dateTime"])
        ends_at = datetime.fromisoformat(end["dateTime"])
        # Google omits the UTC offset when timeZone is given: read the wall time in that zone.
        end_zone = _zone(end.get("timeZone"), zone_name)[0] if end.get("timeZone") else zone
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=zone)
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=end_zone)
    elif "date" in start and "date" in end:
        # All-day events: Google's end date is exclusive, so midnight to midnight.
        starts_at = datetime.combine(date.fromisoformat(start["date"]), time.min, zone)
        ends_at = datetime.combine(date.fromisoformat(end["date"]), time.min, zone)
    else:
        return None
    if ends_at <= starts_at:
        return None
    return starts_at, ends_at, zone_name


def _fetch_items(service, calendar_id, time_min, time_max):
    request = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        showDeleted=True,
        maxResults=250,
    )
    calendar_timezone = settings.TIME_ZONE
    items = []
    while request is not None:
        response = request.execute()
        calendar_timezone = response.get("timeZone", calendar_timezone)
        items.extend(response.get("items", []))
        request = service.events().list_next(request, response)
    return items, calendar_timezone


def sync_connection(connection, now=None):
    """Mirror the connection's Google events in the import window into Syncmily.

    Raises google.auth.exceptions.RefreshError when Google no longer accepts the
    stored token, and googleapiclient.errors.HttpError for other API failures.
    """
    now = now or timezone.now()
    time_min = now - timedelta(days=PAST_DAYS)
    time_max = now + timedelta(days=FUTURE_DAYS)
    credentials = _credentials(connection)
    items, calendar_timezone = _fetch_items(
        _service(credentials), connection.calendar_id, time_min, time_max
    )

    result = SyncResult()
    with transaction.atomic():
        existing = {event.external_id: event for event in connection.events.all()}
        seen = set()
        for item in items:
            external_id = item.get("id")
            if not external_id or item.get("status") == "cancelled":
                continue
            times = _event_times(item, calendar_timezone)
            if times is None:
                result.skipped += 1
                continue
            starts_at, ends_at, timezone_name = times
            fields = {
                "workspace_id": connection.workspace_id,
                "title": (item.get("summary") or "").strip()[:200] or "(No title)",
                "starts_at": starts_at,
                "ends_at": ends_at,
                "timezone_name": timezone_name,
                "location": (item.get("location") or "").strip()[:255],
            }
            seen.add(external_id)
            event = existing.get(external_id)
            if event is None:
                Event.objects.create(
                    created_by=connection.user,
                    workspace=connection.workspace,
                    source=Event.Source.GOOGLE,
                    calendar_connection=connection,
                    external_id=external_id,
                    **fields,
                )
                result.created += 1
            elif any(getattr(event, name) != value for name, value in fields.items()):
                for name, value in fields.items():
                    setattr(event, name, value)
                event.save(update_fields=[*fields, "updated_at"])
                result.updated += 1

        # Remove imported events in the window that Google no longer lists.
        stale = connection.events.filter(
            ends_at__gt=time_min, starts_at__lt=time_max
        ).exclude(external_id__in=seen)
        result.deleted, _ = stale.delete()

        connection.last_synced_at = now
        update_fields = ["last_synced_at", "updated_at"]
        if credentials.token and credentials.token != connection.access_token:
            connection.access_token = credentials.token
            expiry = credentials.expiry
            connection.token_expires_at = (
                expiry.replace(tzinfo=datetime_timezone.utc) if expiry else None
            )
            update_fields += ["access_token", "token_expires_at"]
        connection.save(update_fields=update_fields)
    return result
