from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from events.models import Event
from syncmily import limits
from syncmily.safety import validate_event_window
from syncmily.text import clean_visible_text, truncate_graphemes

from .models import CalendarConnection

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
        expiry=(
            expiry.astimezone(datetime_timezone.utc).replace(tzinfo=None)
            if expiry
            else None
        ),
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
    zone, zone_name = _zone(
        start.get("timeZone") or calendar_timezone, settings.TIME_ZONE
    )
    if "dateTime" in start and "dateTime" in end:
        starts_at = datetime.fromisoformat(start["dateTime"])
        ends_at = datetime.fromisoformat(end["dateTime"])
        # Google omits the UTC offset when timeZone is given: read the wall time in that
        # zone.
        end_zone = (
            _zone(end.get("timeZone"), zone_name)[0] if end.get("timeZone") else zone
        )
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


class FetchedItems(list):
    """The events Google returned. `truncated` is set when more pages were left."""

    truncated = False


def _fetch_items(service, calendar_id, time_min, time_max):
    request = service.events().list(
        calendarId=calendar_id,
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        showDeleted=True,
        maxResults=limits.QUOTAS.google_page_size,
    )
    calendar_timezone = settings.TIME_ZONE
    items = FetchedItems()
    pages = 0
    while request is not None:
        if pages >= limits.QUOTAS.google_pages_per_sync:
            items.truncated = True
            break
        response = request.execute()
        pages += 1
        calendar_timezone = response.get("timeZone", calendar_timezone)
        items.extend(response.get("items", []))
        request = service.events().list_next(request, response)
    return items, calendar_timezone


def _imported_text(value, field_name):
    """Clean text from Google, cut at a whole character to fit the Event column."""
    max_length = Event._meta.get_field(field_name).max_length
    return truncate_graphemes(clean_visible_text(value), max_length)


def _import_window_ok(starts_at, ends_at, zone_name):
    """True when the plan sits inside 1900-2100 (a long plan is fine for an import)."""
    try:
        validate_event_window(starts_at, ends_at, ZoneInfo(zone_name))
    except ValidationError as error:
        return error.code == "too_long"
    except (OverflowError, ValueError):
        return False
    return True


def sync_connection(connection, now=None):
    """Mirror the connection's Google events in the import window into Syncmily.

    Raises google.auth.exceptions.RefreshError when Google no longer accepts the
    stored token, and googleapiclient.errors.HttpError for other API failures.
    A run that overlaps another run on the same connection returns at once.
    """
    now = now or timezone.now()
    time_min = now - timedelta(days=PAST_DAYS)
    time_max = now + timedelta(days=FUTURE_DAYS)
    result = SyncResult()
    with transaction.atomic():
        locked = (
            CalendarConnection.objects.select_for_update(skip_locked=True)
            .only("pk")
            .filter(pk=connection.pk)
            .first()
        )
        if locked is None:
            return result
        credentials = _credentials(connection)
        items, calendar_timezone = _fetch_items(
            _service(credentials), connection.calendar_id, time_min, time_max
        )
        too_many = bool(getattr(items, "truncated", False))
        ceiling = limits.QUOTAS.google_events_per_connection
        existing = {event.external_id: event for event in connection.events.all()}
        stored = len(existing)
        seen = set()
        for item in items:
            external_id = item.get("id")
            if not isinstance(external_id, str) or not external_id:
                continue
            if item.get("status") == "cancelled":
                continue
            if len(external_id) > limits.TEXT.google_external_id:
                result.skipped += 1
                continue
            event = existing.get(external_id)
            if event is None and stored >= ceiling:
                too_many = True
                break
            try:
                # A bad item skips only itself: its savepoint rolls back on its own.
                with transaction.atomic():
                    outcome = _import_item(
                        connection, item, calendar_timezone, existing, event
                    )
            except Exception:  # noqa: BLE001 - one odd event never stops the sync
                result.skipped += 1
                continue
            if outcome is None:
                result.skipped += 1
                continue
            seen.add(external_id)
            if outcome == "created":
                result.created += 1
                stored += 1
            elif outcome == "updated":
                result.updated += 1

        if not too_many:
            # Remove imported events in the window that Google no longer lists.
            stale = connection.events.filter(
                ends_at__gt=time_min, starts_at__lt=time_max
            ).exclude(external_id__in=seen)
            result.deleted, _ = stale.delete()

        connection.last_synced_at = now
        connection.sync_error = (
            CalendarConnection.SyncError.TOO_MANY
            if too_many
            else CalendarConnection.SyncError.NONE
        )
        connection.sync_failed_at = now if too_many else None
        update_fields = ["last_synced_at", "sync_error", "sync_failed_at", "updated_at"]
        if credentials.token and credentials.token != connection.access_token:
            connection.access_token = credentials.token
            expiry = credentials.expiry
            connection.token_expires_at = (
                expiry.replace(tzinfo=datetime_timezone.utc) if expiry else None
            )
            update_fields += ["access_token", "token_expires_at"]
        connection.save(update_fields=update_fields)
    return result


def _import_item(connection, item, calendar_timezone, existing, event):
    """Create or update one imported event. Return "created", "updated", "same" or
    None when the item can't be used."""
    times = _event_times(item, calendar_timezone)
    if times is None:
        return None
    starts_at, ends_at, timezone_name = times
    if not _import_window_ok(starts_at, ends_at, timezone_name):
        return None
    fields = {
        "workspace_id": connection.workspace_id,
        # Anyone can put an event on a Google calendar, so its text gets the same
        # normalizer as typed text before the fallback and the cut.
        "title": _imported_text(item.get("summary"), "title") or "(No title)",
        "starts_at": starts_at,
        "ends_at": ends_at,
        "timezone_name": timezone_name,
        "location": _imported_text(item.get("location"), "location"),
        # Google marks unconfirmed events "tentative"; keep that on our side.
        "is_tentative": item.get("status") == "tentative",
    }
    external_id = item["id"]
    if event is None:
        existing[external_id] = Event.objects.create(
            created_by=connection.user,
            workspace=connection.workspace,
            source=Event.Source.GOOGLE,
            calendar_connection=connection,
            external_id=external_id,
            **fields,
        )
        return "created"
    if any(getattr(event, name) != value for name, value in fields.items()):
        for name, value in fields.items():
            setattr(event, name, value)
        event.save(update_fields=[*fields, "updated_at"])
        return "updated"
    return "same"


def record_sync_failure(connection, error, now=None):
    """Remember why the last import failed, so the family page can say so.

    Only the error kind is stored: never the exception text, which can echo request
    details.
    """
    # A token that can't be decrypted can't be used either: only reconnecting fixes it.
    if isinstance(error, (RefreshError, ImproperlyConfigured)):
        kind = CalendarConnection.SyncError.ACCESS_REVOKED
    else:
        kind = CalendarConnection.SyncError.FAILED
    now = now or timezone.now()
    # A queryset update leaves the connection's other fields (fresh tokens, the
    # auto-sync switch) as they are in the database.
    CalendarConnection.objects.filter(pk=connection.pk).update(
        sync_error=kind, sync_failed_at=now
    )
    connection.sync_error = kind
    connection.sync_failed_at = now
    return kind
