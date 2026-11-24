import logging
from datetime import time as dt_time
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.core.exceptions import BadRequest
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_cache_control
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.models import MemberColor
from syncmily.localtime import localtime_or_utc
from syncmily.errors import json_endpoint, json_error
from syncmily.limits import RATES, RENDER
from syncmily.locking import CONFLICT_MESSAGE, expected_version, remember_version
from syncmily.ratelimit import allow, too_many_requests
from syncmily.safety import Params, enforce_quota

from .access import family_events, family_workspaces
from .ai_forms import AIEventSuggestionForm
from .ai_suggestions import (
    EventSuggestionError,
    ai_enabled,
    generate_event_suggestion,
)
from .day import vertical_day
from .forms import (
    EventDriverForm,
    EventForm,
    EventScheduleForm,
    EventTimeForm,
    EventTimezoneForm,
    EventWorkspaceForm,
    MemberColorsForm,
    PlaceDetailsForm,
    PlaceQueryForm,
    PrefillForm,
    TransportModeForm,
    TripStartForm,
)
from .models import Event, TripStart
from .services.event_ocr import EventOCRError, ocr_available, scan_event_image
from .travel_conflicts import find_travel_checks
from . import places
from .trip import build_trip, marker_data
from .timeline import build_day_timeline, editable_members, family_members
from .month import build_month
from .nav import WEEKENDS_SESSION_KEY, period_nav, show_weekends
from .week import local_day_bounds

logger = logging.getLogger(__name__)


class StaleEdit(Exception):
    """The row changed after the edit page was loaded."""


def _event_write_limited(request):
    """A 429 response when this person saves plans too fast, else None. The write
    limits count every save, edit, delete and time change, and run before the quota
    check."""
    for scope in ("event_write_minute", "event_write_hour"):
        rate = RATES[scope]
        if not allow(request, scope, rate.limit, rate.window, rate.key):
            return too_many_requests(request, scope=scope)
    return None


EDITABLE_FIELDS = (
    "title",
    "starts_at",
    "ends_at",
    "timezone_name",
    "location",
    "latitude",
    "longitude",
    "transport_mode",
    "is_tentative",
    "requires_transport",
    "dropoff_driver_id",
    "pickup_driver_id",
)


def _saves_nothing_new(current, edited, participants):
    """True when the edit would leave the saved plan exactly as it is. Someone else's
    save that matches this form is no lost update, so it needs no second look."""
    return all(
        getattr(current, name) == getattr(edited, name) for name in EDITABLE_FIELDS
    ) and set(current.participants.values_list("pk", flat=True)) == {
        person.pk for person in participants
    }


def _usable_zone(name, *moments):
    """The zone name when every moment can be shown there, else "UTC". A plan near
    year 9999 cannot be written on the clock of a zone ahead of UTC."""
    zone = ZoneInfo(name)
    try:
        for moment in moments:
            moment.astimezone(zone)
    except (OverflowError, ValueError):
        return "UTC"
    return name


def _local_day(event_details):
    """The plan's start day on its own wall clock, which is the day the cap counts."""
    zone = ZoneInfo(event_details.timezone_name)
    return localtime_or_utc(event_details.starts_at, zone).date(), zone


@require_http_methods(["GET", "POST"])
def create_event(request, workspace_id=None):
    if request.method == "POST" and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), reverse("accounts:login"))
    if (
        workspace_id is None
        and request.method == "GET"
        and request.user.is_authenticated
    ):
        query = request.GET.urlencode()
        return redirect(f"{reverse('events:choose_workspace')}?{query}")
    workspace = None
    if workspace_id is not None:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("accounts:login"))
        workspace = get_object_or_404(
            family_workspaces(request.user, caregivers_only=True), pk=workspace_id
        )
    can_save = workspace is not None
    action = (
        Params(request.POST).choice(
            "action", ("save", "preview", "generate_suggestion"), default="preview"
        )
        if request.method == "POST"
        else None
    )
    saving = action == "save"

    if action in ("generate_suggestion", "save") and not can_save:
        raise PermissionDenied
    if saving:
        limited = _event_write_limited(request)
        if limited is not None:
            return limited

    ai_form = AIEventSuggestionForm(
        request.POST if action == "generate_suggestion" else None
    )
    ai_missing_fields = []
    ai_suggestion_generated = False

    initial = {}
    if request.method == "GET":
        schedule_form = EventScheduleForm(request.GET)
        if schedule_form.is_valid():
            initial = {
                "timezone": schedule_form.cleaned_data["timezone"],
            }
            if schedule_form.data.get("date"):
                initial["date"] = schedule_form.cleaned_data["date"]
            prefill = PrefillForm(request.GET)
            start_time = prefill.cleaned_data["time"] if prefill.is_valid() else None
            if start_time is not None:
                # One hour by default, kept inside the same day.
                end_minutes = min(
                    start_time.hour * 60 + start_time.minute + 60, 23 * 60 + 59
                )
                initial["start_time"] = start_time
                initial["end_time"] = dt_time(end_minutes // 60, end_minutes % 60)

    if (
        action == "generate_suggestion"
        and ai_form.is_valid()
        and not (
            allow(request, "ai_suggestion_minute", 5, 60)
            and allow(request, "ai_suggestion_hour", 20, 3600)
        )
    ):
        ai_form.add_error(
            None,
            "You have made too many suggestion requests. "
            "Wait a few minutes, or fill in the form yourself.",
        )
    elif action == "generate_suggestion" and ai_form.is_valid():
        try:
            suggestion_timezone = Params(request.POST).timezone(
                "timezone", timezone.get_current_timezone_name()
            )
        except BadRequest:
            suggestion_timezone = None
            ai_form.add_error(None, "Choose a valid timezone.")
        if suggestion_timezone is not None:
            try:
                suggestion = generate_event_suggestion(
                    ai_form.cleaned_data["description"],
                    timezone_name=suggestion_timezone,
                )
            except EventSuggestionError as error:
                ai_form.add_error(None, str(error))
            else:
                initial.update(
                    {
                        key: value
                        for key, value in suggestion.items()
                        if key != "missing_fields" and value is not None
                    }
                )
                initial["timezone"] = suggestion_timezone
                ai_missing_fields = suggestion["missing_fields"]
                ai_suggestion_generated = True

    form = EventForm(
        (
            request.POST
            if request.method == "POST" and action != "generate_suggestion"
            else None
        ),
        initial=initial,
        user=request.user,
        workspace=workspace,
    )
    event_details = None
    travel_checks = []
    timezone_name = timezone.get_current_timezone_name()
    if request.method == "POST" and form.is_valid():
        event_details = form.event
        timezone_name = form.cleaned_data["timezone"]
        travel_checks = find_travel_checks(
            event_details,
            form.cleaned_data["participants"],
        )
        if saving:
            try:
                submission = signing.loads(
                    form.cleaned_data["submission_token"],
                    salt="events.create",
                    max_age=86400,
                )
                if submission["user"] != request.user.pk or submission[
                    "workspace"
                ] != str(workspace.pk):
                    raise signing.BadSignature
                submission_id = UUID(submission["id"])
            except (signing.BadSignature, ValueError, KeyError, TypeError):
                form.add_error(
                    None,
                    "This form has expired or is invalid. Start over and try again.",
                )
            else:
                event_details.submission_id = submission_id
                try:
                    with transaction.atomic():
                        event_details.full_clean(validate_unique=False)
                        already_saved = Event.objects.filter(
                            submission_id=submission_id,
                            created_by=request.user,
                            workspace=workspace,
                        ).exists()
                        if not already_saved:
                            day, zone = _local_day(event_details)
                            enforce_quota(workspace, "events_per_day", day=day, tz=zone)
                            enforce_quota(workspace, "events_rolling_30d")
                        saved_event, created = Event.objects.get_or_create(
                            submission_id=submission_id,
                            created_by=request.user,
                            workspace=workspace,
                            defaults={
                                "title": event_details.title,
                                "starts_at": event_details.starts_at,
                                "ends_at": event_details.ends_at,
                                "location": event_details.location,
                                "timezone_name": event_details.timezone_name,
                                "transport_mode": event_details.transport_mode,
                                "requires_transport": event_details.requires_transport,
                                "dropoff_driver": event_details.dropoff_driver,
                                "pickup_driver": event_details.pickup_driver,
                                "is_tentative": event_details.is_tentative,
                                "latitude": event_details.latitude,
                                "longitude": event_details.longitude,
                            },
                        )
                        if created:
                            saved_event.participants.set(
                                form.cleaned_data["participants"]
                            )
                except ValidationError as error:
                    form.add_error(None, error.messages)
                except DatabaseError:
                    form.add_error(
                        None,
                        "The event could not be saved. Your details are still here."
                        " Please try again.",
                    )
                if not form.errors:
                    messages.success(request, "Your event has been saved.")
                    detail_url = reverse("events:detail", args=[saved_event.pk])
                    return redirect(
                        f"{detail_url}?{urlencode({'timezone': timezone_name})}"
                    )

    return render(
        request,
        "events/create_event.html",
        {
            "form": form,
            "ai_form": ai_form,
            # A posted request still shows the panel, so its answer has somewhere to go.
            "ai_enabled": ai_enabled() or action == "generate_suggestion",
            "ai_missing_fields": ai_missing_fields,
            "ai_suggestion_generated": ai_suggestion_generated,
            "event_details": event_details,
            "timezone_name": timezone_name,
            "can_save": can_save,
            "workspace": workspace,
            "workspace_url": (
                reverse("accounts:workspace_detail", args=[workspace.pk])
                if workspace and workspace.primary_admin_id == request.user.pk
                else reverse("accounts:dashboard")
            ),
            "form_url": (
                reverse("events:workspace_create", args=[workspace.pk])
                if workspace
                else reverse("events:create")
            ),
            "ocr_scan_url": (
                reverse("events:scan_ocr", args=[workspace.pk])
                if can_save and ocr_available()
                else ""
            ),
            "travel_checks": travel_checks,
            "members": (
                {member["user"].pk: member for member in family_members([workspace])}
                if workspace
                else {}
            ),
        },
    )


@login_required(login_url="accounts:login")
@require_http_methods(["GET", "POST"])
def edit_event(request, pk):
    event = get_object_or_404(
        Event.objects.filter(
            workspace__in=family_workspaces(request.user, caregivers_only=True),
            source=Event.Source.SYNCMILY,
        )
        .select_related("workspace")
        .prefetch_related("participants"),
        pk=pk,
    )
    form = EventForm(
        request.POST if request.method == "POST" else None,
        instance=event,
        user=request.user,
    )
    event_details = None
    travel_checks = []
    timezone_name = event.timezone_name
    status = 200
    conflict_message = ""
    if request.method == "POST" and form.is_valid():
        event_details = form.event
        timezone_name = form.cleaned_data["timezone"]
        travel_checks = find_travel_checks(
            event_details, form.cleaned_data["participants"]
        )
        if Params(request.POST).choice("action", ("save", "preview")) == "save":
            limited = _event_write_limited(request)
            if limited is not None:
                return limited
            try:
                with transaction.atomic():
                    current = get_object_or_404(
                        Event.objects.select_for_update(), pk=event.pk
                    )
                    expected = expected_version(
                        request, "event", event.pk, form.cleaned_data.get("version")
                    )
                    if current.version != expected and not _saves_nothing_new(
                        current, event_details, form.cleaned_data["participants"]
                    ):
                        raise StaleEdit
                    event_details.full_clean()
                    day, zone = _local_day(event_details)
                    if day != _local_day(current)[0]:
                        enforce_quota(
                            event.workspace, "events_per_day", day=day, tz=zone
                        )
                    event_details.save(
                        update_fields=[
                            "title",
                            "starts_at",
                            "ends_at",
                            "timezone_name",
                            "location",
                            "latitude",
                            "longitude",
                            "transport_mode",
                            "is_tentative",
                            "requires_transport",
                            "dropoff_driver",
                            "pickup_driver",
                            "updated_at",
                        ]
                    )
                    event_details.participants.set(form.cleaned_data["participants"])
            except StaleEdit:
                # Show the saved plan, so the person can review it and save again.
                conflict_message = CONFLICT_MESSAGE
                event.refresh_from_db()
                remember_version(request, "event", event.pk, event.version)
                form = EventForm(instance=event, user=request.user)
                event_details = None
                travel_checks = []
                timezone_name = event.timezone_name
                status = 409
            except ValidationError as error:
                form.add_error(None, error.messages)
            except DatabaseError:
                form.add_error(
                    None,
                    "The event could not be saved. Your details are still here."
                    " Please try again.",
                )
            else:
                remember_version(request, "event", event.pk, event_details.version)
                messages.success(request, "Your event has been updated.")
                detail_url = reverse("events:detail", args=[event.pk])
                return redirect(
                    f"{detail_url}?{urlencode({'timezone': timezone_name})}"
                )
    elif request.method == "GET":
        remember_version(request, "event", event.pk, event.version)
    return render(
        request,
        "events/create_event.html",
        {
            "form": form,
            "event": event,
            "is_edit": True,
            "event_details": event_details,
            "timezone_name": timezone_name,
            "can_save": True,
            "workspace": event.workspace,
            "workspace_url": (
                reverse("accounts:workspace_detail", args=[event.workspace_id])
                if event.workspace.primary_admin_id == request.user.pk
                else reverse("accounts:dashboard")
            ),
            "form_url": reverse("events:edit", args=[event.pk]),
            "conflict_message": conflict_message,
            "travel_checks": travel_checks,
            "members": {
                member["user"].pk: member
                for member in family_members([event.workspace])
            },
        },
        status=status,
    )


@login_required(login_url="accounts:login")
@require_http_methods(["GET", "POST"])
def delete_event(request, pk):
    event = get_object_or_404(
        Event.objects.filter(
            workspace__in=family_workspaces(request.user, caregivers_only=True),
            source=Event.Source.SYNCMILY,
        ).select_related("workspace"),
        pk=pk,
    )
    timezone_form = EventTimezoneForm(request.GET, zone_default=event.timezone_name)
    timezone_name = event.timezone_name
    if timezone_form.is_valid():
        timezone_name = timezone_form.cleaned_data["timezone"]
    timezone_name = _usable_zone(timezone_name, event.starts_at, event.ends_at)
    timezone_query = urlencode({"timezone": timezone_name})
    error = None
    if request.method == "POST":
        limited = _event_write_limited(request)
        if limited is not None:
            return limited
        action = Params(request.POST).token("action", r"[a-z_]+", 20)
        if action != "delete":
            error = "Choose Delete event to confirm, or Keep event to go back."
        else:
            schedule_query = urlencode(
                {
                    "date": localtime_or_utc(
                        event.starts_at, ZoneInfo(timezone_name)
                    ).date(),
                    "view": "day",
                    "timezone": timezone_name,
                }
            )
            schedule_url = reverse("events:workspace_list", args=[event.workspace_id])
            try:
                with transaction.atomic():
                    event.delete()
            except DatabaseError:
                event.pk = pk
                error = "The event could not be deleted. Please try again."
            else:
                messages.success(request, "Your event has been deleted.")
                return redirect(f"{schedule_url}?{schedule_query}")
    return render(
        request,
        "events/delete_event.html",
        {
            "event": event,
            "timezone_name": timezone_name,
            "timezone_query": timezone_query,
            "error": error,
            "workspace_url": (
                reverse("accounts:workspace_detail", args=[event.workspace_id])
                if event.workspace.primary_admin_id == request.user.pk
                else reverse("accounts:dashboard")
            ),
        },
    )


@login_required(login_url="accounts:login")
@require_GET
def choose_workspace(request):
    form = EventWorkspaceForm.from_query(request.GET, user=request.user)
    if not form.is_bound:
        # One family to add to: nothing to choose, so go straight to the event form.
        choices = list(form.fields["workspace"].queryset[:2])
        if len(choices) == 1:
            form = EventWorkspaceForm(
                {
                    **form.initial,
                    "workspace": choices[0].pk,
                    "date": form.initial["date"] or "",
                    "time": form.initial["time"] or "",
                },
                user=request.user,
            )
    if form.is_bound and form.is_valid():
        workspace = form.cleaned_data["workspace"]
        query = {"timezone": form.cleaned_data["timezone"]}
        if form.cleaned_data["date"]:
            query["date"] = form.cleaned_data["date"]
        if form.cleaned_data["time"]:
            query["time"] = form.cleaned_data["time"].strftime("%H:%M")
        create_url = reverse("events:workspace_create", args=[workspace.pk])
        return redirect(f"{create_url}?{urlencode(query)}")
    return render(request, "events/choose_workspace.html", {"form": form})


@login_required(login_url="accounts:login")
@require_POST
def set_transport_mode(request, pk):
    """A parent changes how a plan is reached straight from the "Your day" pop-up.

    The full event form still offers the same choice; this only saves the one field.
    """
    event = get_object_or_404(
        Event.objects.filter(
            workspace__in=family_workspaces(request.user, caregivers_only=True)
        ),
        pk=pk,
    )
    mode_form = TransportModeForm(request.POST)
    if not mode_form.is_valid():
        return HttpResponseBadRequest("Choose car, subway or bus, walking or cycling.")
    mode = mode_form.cleaned_data["transport_mode"]
    if event.transport_mode != mode:
        event.transport_mode = mode
        event.save(update_fields=["transport_mode", "updated_at"])
    if request.headers.get("X-Requested-With") == "fetch":
        return HttpResponse(status=204)
    return redirect(
        Params(request.POST, request).next_url("next", reverse("accounts:dashboard"))
    )


@login_required(login_url="accounts:login")
@require_http_methods(["GET", "POST"])
def event_detail(request, pk):
    event = get_object_or_404(
        family_events(request.user)
        .select_related(
            "workspace",
            "dropoff_driver",
            "pickup_driver",
        )
        .prefetch_related("participants"),
        pk=pk,
    )
    list_url = (
        reverse("events:workspace_list", args=[event.workspace_id])
        if event.workspace_id
        else reverse("events:list")
    )
    timezone_form = EventTimezoneForm(request.GET, zone_default=event.timezone_name)
    timezone_name = event.timezone_name
    if timezone_form.is_valid():
        timezone_name = timezone_form.cleaned_data["timezone"]
    timezone_name = _usable_zone(timezone_name, event.starts_at, event.ends_at)
    can_manage_drivers = (
        bool(event.workspace_id)
        and family_workspaces(
            request.user,
            caregivers_only=True,
        )
        .filter(pk=event.workspace_id)
        .exists()
    )
    driver_form = None
    if request.method == "POST" and not can_manage_drivers:
        raise PermissionDenied
    if can_manage_drivers:
        driver_form = EventDriverForm(
            request.POST if request.method == "POST" else None,
            instance=event,
        )
        if request.method == "POST":
            if driver_form.is_valid():
                try:
                    with transaction.atomic():
                        event.save(update_fields=[*driver_form.fields, "updated_at"])
                except DatabaseError:
                    driver_form.add_error(
                        None, "Driver assignments could not be saved. Please try again."
                    )
                else:
                    messages.success(request, "Driver assignments saved.")
                    detail_url = reverse("events:detail", args=[event.pk])
                    query = urlencode({"timezone": timezone_name})
                    return redirect(f"{detail_url}?{query}")
            try:
                event.refresh_from_db()
            except Event.DoesNotExist:
                # Someone deleted the event while this form was being saved.
                raise Http404("That event was deleted.")
    local_day = localtime_or_utc(event.starts_at, ZoneInfo(timezone_name)).date()
    schedule_query = urlencode(
        {
            "date": local_day,
            "view": "day",
            "timezone": timezone_name,
        }
    )
    members = (
        {member["user"].pk: member for member in family_members([event.workspace])}
        if event.workspace_id
        else {}
    )
    same_day = []
    try:
        day_start, day_end = local_day_bounds(local_day, ZoneInfo(timezone_name))
    except OverflowError:
        # The last day Python can hold has no next day: nothing else to compare.
        day_start = day_end = None
    if event.workspace_id and day_start is not None:
        same_day = list(
            family_events(request.user)
            .filter(
                workspace_id=event.workspace_id,
                starts_at__lt=day_end,
                ends_at__gt=day_start,
            )
            .exclude(pk=event.pk)
            .prefetch_related("participants")
            .order_by("starts_at", "pk")[:6]
        )
        for other in same_day:
            other.people = [
                members.get(person.pk, {"user": person})
                for person in other.participants.all()
            ]
    return render(
        request,
        "events/event_detail.html",
        {
            "event": event,
            "people": [
                members.get(person.pk, {"user": person})
                for person in event.participants.all()
            ],
            "same_day": same_day,
            "local_day": local_day,
            "schedule_query": schedule_query,
            "list_url": list_url,
            "timezone_form": timezone_form,
            "timezone_name": timezone_name,
            "can_create": can_manage_drivers,
            "can_edit": can_manage_drivers and event.source == Event.Source.SYNCMILY,
            "driver_form": driver_form,
            "starts_input": _local_input(
                event.starts_at, ZoneInfo(event.timezone_name)
            ),
            "ends_input": _local_input(event.ends_at, ZoneInfo(event.timezone_name)),
            # The leave-by card only makes sense for an upcoming event with a map pin.
            "show_trip": event.has_valid_coordinates
            and event.transport_mode == Event.TransportMode.TRANSIT
            and event.starts_at > timezone.now(),
            "workspace_url": (
                reverse("accounts:workspace_detail", args=[event.workspace_id])
                if event.workspace_id
                and event.workspace.primary_admin_id == request.user.pk
                else reverse("accounts:dashboard")
            ),
        },
    )


@login_required(login_url="accounts:login")
@require_GET
def event_list(request, workspace_id=None):
    workspace = None
    if workspace_id is not None:
        workspace = get_object_or_404(family_workspaces(request.user), pk=workspace_id)
    accessible_events = family_events(request.user)
    if workspace:
        accessible_events = accessible_events.filter(workspace=workspace)
    form = EventScheduleForm(request.GET, auto_id="date_menu_%s")
    events = Event.objects.none()
    caregiver_workspaces = family_workspaces(request.user, caregivers_only=True)
    context = {
        "form": form,
        "schedule_form": form,
        "workspace": workspace,
        # New sign-ups land here before they have a family; the page says what to do.
        "has_family": bool(workspace) or family_workspaces(request.user).exists(),
        "timezone_name": timezone.get_current_timezone_name(),
        "can_create": (
            caregiver_workspaces.filter(pk=workspace.pk).exists()
            if workspace
            else caregiver_workspaces.exists()
        ),
        "workspace_url": (
            reverse("accounts:workspace_detail", args=[workspace.pk])
            if workspace and workspace.primary_admin_id == request.user.pk
            else reverse("accounts:dashboard")
        ),
    }
    if form.is_valid():
        period = form.cleaned_data
        events = accessible_events.filter(ends_at__gt=period["starts_at"])
        if period["view"] != "upcoming":
            events = events.filter(starts_at__lt=period["ends_at"])
        events = events.select_related(
            "workspace", "dropoff_driver", "pickup_driver"
        ).order_by("starts_at", "pk")
        timezone_query = {"timezone": period["timezone"]}
        create_query = urlencode({"date": period["date"], **timezone_query})
        context.update(
            timezone_name=period["timezone"],
            timezone_query=urlencode(timezone_query),
            create_query=create_query,
            list_from=period.get("list_from"),
            period_query=urlencode(
                {"date": period["date"], "view": period["view"], **timezone_query}
            ),
        )
        if period["view"] != "upcoming":
            today = timezone.localdate(timezone=ZoneInfo(period["timezone"]))
            context.update(
                is_current=period["start_date"] <= today <= period["last_date"],
                start_date=period["start_date"],
                last_date=period["last_date"],
                previous_query=urlencode(
                    {
                        "date": period["previous_date"],
                        "view": period["view"],
                        **timezone_query,
                    }
                ),
                next_query=urlencode(
                    {
                        "date": period["next_date"],
                        "view": period["view"],
                        **timezone_query,
                    }
                ),
            )
    if form.is_valid():
        period = form.cleaned_data
        context["period_nav"] = period_nav(
            request,
            view=period["view"],
            date=period["date"],
            start=period.get("start_date"),
            last=period.get("last_date"),
            workspace=workspace,
            timezone_name=period["timezone"],
            previous_url=(
                f"?{context['previous_query']}" if "previous_query" in context else None
            ),
            next_url=f"?{context['next_query']}" if "next_query" in context else None,
            today_url=f"?view={period['view']}&{context['timezone_query']}",
            is_current=context.get("is_current", False),
        )
    if form.is_valid() and form.cleaned_data["view"] == "month":
        workspaces = [workspace] if workspace else list(family_workspaces(request.user))
        context["month"] = build_month(
            events.prefetch_related("participants"),
            family_members(workspaces),
            period["start_date"],
            ZoneInfo(period["timezone"]),
            timezone.localdate(timezone=ZoneInfo(period["timezone"])),
            show_weekends(request),
        )
    if form.is_valid() and form.cleaned_data["view"] == "day":
        context.update(
            _day_timeline_context(
                request, workspace, events.prefetch_related("participants"), period
            )
        )
    page = Paginator(events.prefetch_related("participants"), 20).get_page(
        Params(request.GET).page()
    )
    if not context.get("timeline"):
        _add_people(page, workspace, request.user)
    context["page"] = page
    return render(request, "events/event_list.html", context)


def _day_timeline_context(request, workspace, events, period):
    workspaces = [workspace] if workspace else list(family_workspaces(request.user))
    members = family_members(workspaces)
    tz = ZoneInfo(period["timezone"])
    # A page draws at most 100 plans; the rest are counted ("+N more").
    shown = list(events[: RENDER.plans_per_response])
    more = (
        max(events.count() - len(shown), 0)
        if len(shown) == RENDER.plans_per_response
        else 0
    )
    timeline = build_day_timeline(
        shown, members, period["starts_at"], period["ends_at"], tz, timezone.now()
    )
    # Colours are chosen per family, so the editor needs a single family on screen.
    color_workspace = workspaces[0] if len(workspaces) == 1 else None
    return {
        "timeline": timeline,
        "day": vertical_day(timeline, tz),
        "more_plans": more,
        "week_query": urlencode(
            {
                "date": period["date"],
                "view": "week",
                "timezone": period["timezone"],
            }
        ),
        "color_workspace": color_workspace,
        "color_members": (
            editable_members(request.user, color_workspace, members)
            if color_workspace
            else []
        ),
    }


def _add_people(page, workspace, user):
    """Give each listed event its family members (hue, picked colour) so rows print in
    their colours.
    """
    workspaces = [workspace] if workspace else list(family_workspaces(user))
    members = {member["user"].pk: member for member in family_members(workspaces)}
    for event in page:
        event.people = [
            members.get(participant.pk, {"user": participant})
            for participant in event.participants.all()
        ]


@login_required(login_url="accounts:login")
@require_POST
def member_colors(request, workspace_id):
    """Let a parent save any family member's schedule colour."""
    workspace = get_object_or_404(family_workspaces(request.user), pk=workspace_id)
    allowed = [
        str(member["user"].pk)
        for member in editable_members(
            request.user, workspace, family_members([workspace])
        )
    ]
    if not allowed:
        raise PermissionDenied("Only parents can change schedule colours.")
    form = MemberColorsForm(request.POST, member_ids=allowed)
    if form.names_other_members():
        raise PermissionDenied("You can change only your own family's colours.")
    if not form.is_valid():
        return HttpResponseBadRequest("Choose a colour like #c62828.")
    changes = form.colors()
    for user_id, color in changes.items():
        MemberColor.objects.update_or_create(
            workspace=workspace, user_id=user_id, defaults={"color": color}
        )
    if changes:
        messages.success(request, "Schedule colours saved.")
    return redirect(
        Params(request.POST, request).next_url(
            "next", reverse("events:workspace_list", args=[workspace.pk]) + "?view=day"
        )
    )


@login_required(login_url="accounts:login")
@require_POST
def toggle_weekends(request):
    """Show or hide Saturday and Sunday on the week and month views, per session."""
    request.session[WEEKENDS_SESSION_KEY] = show_weekends(request)
    return redirect(
        Params(request.POST, request).next_url("next", reverse("accounts:dashboard"))
    )


@require_GET
def timezone_names(request):
    """Every time zone, for the "Show all time zones" button beside a time-zone menu."""
    response = JsonResponse(sorted(available_timezones()), safe=False)
    patch_cache_control(response, public=True, max_age=86400)
    return response


@login_required(login_url="accounts:login")
@require_http_methods(["GET", "POST"])
def event_trip(request, pk):
    """The live leave-by card for an event (or ?format=json for the timeline marker).

    POST saves where this person starts the trip: their previous event, the family's
    home, or somewhere else (an address they searched or the browser's location, sent
    in the body so it never lands in URLs or server logs).
    """
    event = get_object_or_404(family_events(request.user), pk=pk)
    if request.method == "GET":
        # The card is cached by the minute, but a page that polls every second still
        # costs a query or two each time: cap it per person.
        rate = RATES["trip_view"]
        if not allow(request, "trip_view", rate.limit, rate.window, rate.key):
            return too_many_requests(request, scope="trip_view")
    if request.method == "POST":
        form = TripStartForm(request.POST)
        if not form.is_valid():
            return HttpResponseBadRequest(_trip_start_problem(form))
        mode = form.cleaned_data["mode"]
        fields = {"mode": mode, "label": "", "latitude": None, "longitude": None}
        if mode == TripStart.Mode.HERE:
            # A new place means fresh (paid) Routes lookups; the other modes start
            # from fixed places whose trips are cached. People pick a place a few
            # times per event, so this only stops a script. It counts per account and
            # per address: new accounts from one address share one budget.
            rate = RATES["trip_start"]
            if not allow(
                request, "trip_start", TRIP_STARTS_PER_HOUR, rate.window, rate.key
            ):
                return too_many_requests(
                    request,
                    "You've changed where trips start a lot. Wait a few minutes.",
                    scope="trip_start",
                )
            fields.update(
                latitude=form.cleaned_data["latitude"],
                longitude=form.cleaned_data["longitude"],
                label=form.cleaned_data["label"] or "Your current location",
            )
        TripStart.objects.update_or_create(
            user=request.user, event=event, defaults=fields
        )
    trip = build_trip(event, request.user)
    if Params(request.GET).choice("format", {"json"}) == "json":
        response = JsonResponse(marker_data(trip))
    else:
        response = render(request, "events/_trip_card.html", trip)
    patch_cache_control(response, private=True, no_store=True)
    return response


# Searching costs money once Google Places is on; this is plenty for a person typing.
PLACE_SEARCHES_PER_MINUTE = RATES["places"].limit
# Changing where a trip starts asks Google Routes again (see event_trip).
TRIP_STARTS_PER_HOUR = RATES["trip_start"].limit


def _trip_start_problem(form):
    """The sentence for a refused trip start."""
    if "mode" in form.errors:
        return "Choose where the trip starts."
    if "latitude" in form.errors or "longitude" in form.errors:
        return "That location isn't valid."
    if "label" in form.errors:
        return "That place name isn't valid."
    return " ".join(form.non_field_errors()) or "That location isn't valid."


def _places_refused(request):
    """The JSON answer for someone who may not use the paid Places lookups, or None.

    Only accepted members of a family search places (the create-event form and the
    trip card are the only screens that ask). This runs before the Maps switch, so a
    throwaway account learns nothing about the server's setup.
    """
    if not family_workspaces(request.user).exists():
        return json_error(403, "forbidden", "Join a family to search for places.")
    rate = RATES["places"]
    if not allow(request, "places", PLACE_SEARCHES_PER_MINUTE, rate.window, rate.key):
        return JsonResponse({"error": "slow down"}, status=429)
    return None


@json_endpoint(["POST"])
def place_suggestions(request):
    """Google Places suggestions for the location box (POST keeps addresses out of
    logs). 503 means Google Places isn't set up, so the box uses its free search.
    """
    refused = _places_refused(request)
    if refused is not None:
        return refused
    if not places.enabled():
        return JsonResponse({"error": "unavailable"}, status=503)
    form = PlaceQueryForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"suggestions": []})
    query = form.cleaned_data["q"]
    if len(query) < 3:
        return JsonResponse({"suggestions": []})
    try:
        suggestions = places.suggest(query, form.cleaned_data["session"])
    except places.PlacesUnavailable:
        return JsonResponse({"error": "unavailable"}, status=503)
    response = JsonResponse({"provider": "google", "suggestions": suggestions})
    patch_cache_control(response, private=True, no_store=True)
    return response


@json_endpoint(["POST"])
def place_details(request):
    """The exact coordinates of the suggestion someone picked."""
    refused = _places_refused(request)
    if refused is not None:
        return refused
    form = PlaceDetailsForm(request.POST)
    if not form.is_valid():
        return json_error(
            400, "invalid_request", "Choose a place from the suggestions."
        )
    if not places.enabled():
        return JsonResponse({"error": "unavailable"}, status=503)
    try:
        place = places.details(form.cleaned_data["id"], form.cleaned_data["session"])
    except places.PlacesUnavailable:
        return JsonResponse({"error": "unavailable"}, status=503)
    if place is None:
        return JsonResponse({"error": "no location"}, status=404)
    response = JsonResponse(place)
    patch_cache_control(response, private=True, no_store=True)
    return response


def _local_input(moment, tz):
    """The value a datetime-local box shows: 2026-11-11T20:52, in the event's zone."""
    return localtime_or_utc(moment, tz).strftime("%Y-%m-%dT%H:%M")


@login_required(login_url="accounts:login")
@require_POST
def change_event_time(request, pk):
    """Change when an event happens, straight from the time on its page. Same rule as
    the Edit event form: a parent in the family, and not a Google Calendar copy."""
    event = get_object_or_404(
        Event.objects.filter(
            workspace__in=family_workspaces(request.user, caregivers_only=True),
            source=Event.Source.SYNCMILY,
        ),
        pk=pk,
    )
    detail_url = reverse("events:detail", args=[event.pk])
    limited = _event_write_limited(request)
    if limited is not None:
        return limited
    # Date and time come as separate boxes (the time follows the 12h/24h switch).
    # Same reading as the Edit event form: a time in the spring-forward gap is an
    # error that names the clock change; a fall-back time is its first occurrence.
    form = EventTimeForm(request.POST, zone=ZoneInfo(event.timezone_name))
    if not form.is_valid():
        problems = form.non_field_errors()
        messages.error(
            request, " ".join(problems) if problems else "Choose a start and end time."
        )
        return redirect(detail_url)
    event.starts_at, event.ends_at = (
        form.cleaned_data["starts_at"],
        form.cleaned_data["ends_at"],
    )
    try:
        event.full_clean(validate_unique=False)
        event.save(update_fields=["starts_at", "ends_at", "updated_at"])
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    except DatabaseError:
        messages.error(request, "The new time couldn't be saved. Please try again.")
    else:
        messages.success(request, "Time changed.")
    return redirect(detail_url)


@json_endpoint(["POST"])
def scan_event_ocr(request, workspace_id):
    """Extract structured draft event details from an uploaded document image."""
    get_object_or_404(
        family_workspaces(request.user, caregivers_only=True), pk=workspace_id
    )
    try:
        ocr_timezone = Params(request.POST).timezone(
            "timezone", default=timezone.get_current_timezone_name()
        )
    except BadRequest:
        return JsonResponse(
            {
                "status": "error",
                "error_code": "invalid_timezone",
                "error": "Choose a valid timezone.",
            },
            status=400,
        )

    if not (
        allow(request, "ocr_scan_minute", 5, 60)
        and allow(request, "ocr_scan_hour", 30, 3600)
    ):
        return too_many_requests(
            request, "You have scanned a lot of images. Wait a few minutes."
        )

    uploaded_file = request.FILES.get("document") or request.FILES.get("image")
    try:
        result = scan_event_image(
            uploaded_file, timezone_name=ocr_timezone, owner=request.user.pk
        )
    except EventOCRError as error:
        return JsonResponse(
            {
                "status": "error",
                "error_code": error.code,
                "error": error.message,
            },
            status=error.http_status,
        )
    except Exception:
        logger.exception("Unexpected error during OCR event scan.")
        return JsonResponse(
            {
                "status": "error",
                "error_code": "ocr_failed",
                "error": (
                    "We couldn't read this image right now. "
                    "Try again or enter the event manually."
                ),
            },
            status=500,
        )
    return JsonResponse(result)
