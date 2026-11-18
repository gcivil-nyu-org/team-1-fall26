import logging
import math
from datetime import datetime, time as dt_time
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.cache import patch_cache_control
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.models import MemberColor
from syncmily.ratelimit import allow, too_many_requests

from .access import family_events, family_workspaces
from .ai_forms import AIEventSuggestionForm
from .ai_suggestions import EventSuggestionError, generate_event_suggestion
from .day import vertical_day
from .forms import (
    EventDriverForm,
    EventForm,
    EventScheduleForm,
    EventTimezoneForm,
    EventWorkspaceForm,
)
from .models import Event, TripStart
from .services.event_ocr import EventOCRError, ocr_available, scan_event_image
from .travel_conflicts import find_travel_checks
from . import places
from .trip import build_trip, marker_data
from .timeline import HEX_COLOR, build_day_timeline, editable_members, family_members
from .week import local_day_bounds

logger = logging.getLogger(__name__)


def _prefill_time(value):
    """The start time a link asks the event form for, or None if missing or invalid."""
    if not value:
        return None
    try:
        return forms.TimeField().clean(value)
    except ValidationError:
        return None


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
    action = request.POST.get("action") if request.method == "POST" else None
    saving = action == "save"

    if action in ("generate_suggestion", "save") and not can_save:
        raise PermissionDenied

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
            if request.GET.get("date"):
                initial["date"] = schedule_form.cleaned_data["date"]
            start_time = _prefill_time(request.GET.get("time"))
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
        suggestion_timezone = (
            request.POST.get("timezone") or timezone.get_current_timezone_name()
        )

        if suggestion_timezone not in available_timezones():
            ai_form.add_error(None, "Choose a valid timezone.")
        else:
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
    if request.method == "POST" and form.is_valid():
        event_details = form.event
        timezone_name = form.cleaned_data["timezone"]
        travel_checks = find_travel_checks(
            event_details, form.cleaned_data["participants"]
        )
        if request.POST.get("action") == "save":
            try:
                with transaction.atomic():
                    event_details.full_clean()
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
            except ValidationError as error:
                form.add_error(None, error.messages)
            except DatabaseError:
                form.add_error(
                    None,
                    "The event could not be saved. Your details are still here."
                    " Please try again.",
                )
            else:
                messages.success(request, "Your event has been updated.")
                detail_url = reverse("events:detail", args=[event.pk])
                return redirect(
                    f"{detail_url}?{urlencode({'timezone': timezone_name})}"
                )
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
            "travel_checks": travel_checks,
            "members": {
                member["user"].pk: member
                for member in family_members([event.workspace])
            },
        },
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
    timezone_data = request.GET.copy()
    if not timezone_data.get("timezone"):
        timezone_data["timezone"] = event.timezone_name
    timezone_form = EventTimezoneForm(timezone_data)
    timezone_name = event.timezone_name
    if timezone_form.is_valid():
        timezone_name = timezone_form.cleaned_data["timezone"]
    timezone_query = urlencode({"timezone": timezone_name})
    error = None
    if request.method == "POST":
        if request.POST.get("action") != "delete":
            error = "Choose Delete event to confirm, or Keep event to go back."
        else:
            schedule_query = urlencode(
                {
                    "date": timezone.localtime(
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
    form = EventWorkspaceForm(
        request.GET if "workspace" in request.GET else None,
        user=request.user,
        initial={
            "date": request.GET.get("date"),
            "time": request.GET.get("time"),
            "timezone": (
                request.GET.get("timezone") or timezone.get_current_timezone_name()
            ),
        },
    )
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
    mode = request.POST.get("transport_mode", "")
    if mode not in Event.TransportMode.values:
        return HttpResponseBadRequest("Choose car, subway or bus, walking or cycling.")
    if event.transport_mode != mode:
        event.transport_mode = mode
        event.save(update_fields=["transport_mode", "updated_at"])
    if request.headers.get("X-Requested-With") == "fetch":
        return HttpResponse(status=204)
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, {request.get_host()}, request.is_secure()
    ):
        next_url = reverse("accounts:dashboard")
    return redirect(next_url)


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
    timezone_data = request.GET.copy()
    if not timezone_data.get("timezone"):
        timezone_data["timezone"] = event.timezone_name
    timezone_form = EventTimezoneForm(timezone_data)
    timezone_name = event.timezone_name
    if timezone_form.is_valid():
        timezone_name = timezone_form.cleaned_data["timezone"]
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
            event.refresh_from_db()
    local_day = timezone.localtime(event.starts_at, ZoneInfo(timezone_name)).date()
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
    if event.workspace_id:
        day_start, day_end = local_day_bounds(local_day, ZoneInfo(timezone_name))
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
    if form.is_valid() and form.cleaned_data["view"] == "day":
        context.update(
            _day_timeline_context(
                request, workspace, events.prefetch_related("participants"), period
            )
        )
    page = Paginator(events.prefetch_related("participants"), 20).get_page(
        request.GET.get("page")
    )
    if not context.get("timeline"):
        _add_people(page, workspace, request.user)
    context["page"] = page
    return render(request, "events/event_list.html", context)


def _day_timeline_context(request, workspace, events, period):
    workspaces = [workspace] if workspace else list(family_workspaces(request.user))
    members = family_members(workspaces)
    tz = ZoneInfo(period["timezone"])
    timeline = build_day_timeline(
        events, members, period["starts_at"], period["ends_at"], tz, timezone.now()
    )
    # Colours are chosen per family, so the editor needs a single family on screen.
    color_workspace = workspaces[0] if len(workspaces) == 1 else None
    return {
        "timeline": timeline,
        "day": vertical_day(timeline, tz),
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
    allowed = {
        str(member["user"].pk)
        for member in editable_members(
            request.user, workspace, family_members([workspace])
        )
    }
    if not allowed:
        raise PermissionDenied("Only parents can change schedule colours.")
    changes = {}
    for key, value in request.POST.items():
        if not key.startswith("color-"):
            continue
        user_id = key.removeprefix("color-")
        if user_id not in allowed:
            raise PermissionDenied("You can change only your own family's colours.")
        if not HEX_COLOR.fullmatch(value):
            return HttpResponseBadRequest("Choose a colour like #c62828.")
        changes[user_id] = value.lower()
    for user_id, color in changes.items():
        MemberColor.objects.update_or_create(
            workspace=workspace, user_id=user_id, defaults={"color": color}
        )
    if changes:
        messages.success(request, "Schedule colours saved.")
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, {request.get_host()}, request.is_secure()
    ):
        next_url = reverse("events:workspace_list", args=[workspace.pk]) + "?view=day"
    return redirect(next_url)


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
    if request.method == "POST":
        mode = request.POST.get("mode", TripStart.Mode.HERE)
        if mode not in TripStart.Mode.values:
            return HttpResponseBadRequest("Choose where the trip starts.")
        fields = {"mode": mode, "label": "", "latitude": None, "longitude": None}
        if mode == TripStart.Mode.HERE:
            try:
                latitude = float(request.POST["latitude"])
                longitude = float(request.POST["longitude"])
            except (KeyError, ValueError):
                return HttpResponseBadRequest("A location is required.")
            if not (
                math.isfinite(latitude)
                and math.isfinite(longitude)
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                return HttpResponseBadRequest("That location isn't valid.")
            label = request.POST.get("label", "").strip()[:255]
            fields.update(
                latitude=latitude,
                longitude=longitude,
                label=label or "Your current location",
            )
        TripStart.objects.update_or_create(
            user=request.user, event=event, defaults=fields
        )
    trip = build_trip(event, request.user)
    if request.GET.get("format") == "json":
        response = JsonResponse(marker_data(trip))
    else:
        response = render(request, "events/_trip_card.html", trip)
    patch_cache_control(response, private=True, no_store=True)
    return response


# Searching costs money once Google Places is on; this is plenty for a person typing.
PLACE_SEARCHES_PER_MINUTE = 60


def _place_search_allowed(user):
    key = f"place-searches:{user.pk}:{int(timezone.now().timestamp() // 60)}"
    count = cache.get_or_set(key, 0, 90)
    if count >= PLACE_SEARCHES_PER_MINUTE:
        return False
    cache.incr(key)
    return True


@login_required(login_url="accounts:login")
@require_POST
def place_suggestions(request):
    """Google Places suggestions for the location box (POST keeps addresses out of
    logs). 503 means Google Places isn't set up, so the box uses its free search.
    """
    if not places.enabled():
        return JsonResponse({"error": "unavailable"}, status=503)
    query = request.POST.get("q", "").strip()[:200]
    if len(query) < 3:
        return JsonResponse({"suggestions": []})
    if not _place_search_allowed(request.user):
        return JsonResponse({"error": "slow down"}, status=429)
    try:
        suggestions = places.suggest(query, request.POST.get("session", ""))
    except places.PlacesUnavailable:
        return JsonResponse({"error": "unavailable"}, status=503)
    response = JsonResponse({"provider": "google", "suggestions": suggestions})
    patch_cache_control(response, private=True, no_store=True)
    return response


@login_required(login_url="accounts:login")
@require_POST
def place_details(request):
    """The exact coordinates of the suggestion someone picked."""
    if not places.enabled():
        return JsonResponse({"error": "unavailable"}, status=503)
    place_id = request.POST.get("id", "")
    if not places.valid_place_id(place_id):
        return HttpResponseBadRequest("Choose a place from the suggestions.")
    if not _place_search_allowed(request.user):
        return JsonResponse({"error": "slow down"}, status=429)
    try:
        place = places.details(place_id, request.POST.get("session", ""))
    except places.PlacesUnavailable:
        return JsonResponse({"error": "unavailable"}, status=503)
    if place is None:
        return JsonResponse({"error": "no location"}, status=404)
    response = JsonResponse(place)
    patch_cache_control(response, private=True, no_store=True)
    return response


def _local_input(moment, tz):
    """The value a datetime-local box shows: 2026-11-11T20:52, in the event's zone."""
    return timezone.localtime(moment, tz).strftime("%Y-%m-%dT%H:%M")


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
    tz = ZoneInfo(event.timezone_name)

    def moment(prefix):
        # Date and time come as separate boxes (the time follows the 12h/24h switch).
        when = f"{request.POST[prefix + '_date']}T{request.POST[prefix + '_time']}"
        return datetime.fromisoformat(when).replace(tzinfo=tz)

    try:
        starts_at, ends_at = moment("starts"), moment("ends")
    except (KeyError, ValueError):
        messages.error(request, "Choose a start and end time.")
        return redirect(detail_url)
    if ends_at <= starts_at:
        messages.error(request, "The end has to be after the start.")
        return redirect(detail_url)
    event.starts_at, event.ends_at = starts_at, ends_at
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


@login_required(login_url="accounts:login")
@require_POST
def scan_event_ocr(request, workspace_id):
    """Extract structured draft event details from an uploaded document image."""
    get_object_or_404(
        family_workspaces(request.user, caregivers_only=True), pk=workspace_id
    )
    ocr_timezone = (
        request.POST.get("timezone") or ""
    ).strip() or timezone.get_current_timezone_name()
    if ocr_timezone not in available_timezones():
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
        result = scan_event_image(uploaded_file, timezone_name=ocr_timezone)
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
