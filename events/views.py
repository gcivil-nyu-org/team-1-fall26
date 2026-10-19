from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from accounts.models import Workspace

from .forms import EventForm, EventScheduleForm, EventTimezoneForm, EventWorkspaceForm
from .models import Event


@require_http_methods(["GET", "POST"])
def create_event(request, workspace_id=None):
    if workspace_id is None and request.method == "GET" and request.user.is_authenticated:
        query = request.GET.urlencode()
        return redirect(f"{reverse('events:choose_workspace')}?{query}")
    workspace = None
    if workspace_id is not None:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("admin:login"))
        workspace = get_object_or_404(Workspace, pk=workspace_id, primary_admin=request.user)
    can_save = workspace is not None
    saving = request.method == "POST" and request.POST.get("action") == "save"
    if saving and not can_save:
        raise PermissionDenied

    initial = {}
    if request.method == "GET":
        schedule_form = EventScheduleForm(request.GET)
        if schedule_form.is_valid():
            initial = {
                "date": schedule_form.cleaned_data["date"],
                "timezone": schedule_form.cleaned_data["timezone"],
            }
    form = EventForm(
        request.POST if request.method == "POST" else None,
        initial=initial,
        user=request.user,
        workspace=workspace,
    )
    event_details = None
    timezone_name = timezone.get_current_timezone_name()
    if request.method == "POST" and form.is_valid():
        event_details = form.event
        timezone_name = form.cleaned_data["timezone"]
        if saving:
            try:
                submission = signing.loads(
                    form.cleaned_data["submission_token"],
                    salt="events.create",
                    max_age=86400,
                )
                if (
                    submission["user"] != request.user.pk
                    or submission["workspace"] != str(workspace.pk)
                ):
                    raise signing.BadSignature
                submission_id = UUID(submission["id"])
            except (signing.BadSignature, ValueError, KeyError, TypeError):
                form.add_error(None, "This form has expired or is invalid. Start over and try again.")
            else:
                event_details.submission_id = submission_id
                try:
                    with transaction.atomic():
                        event_details.full_clean(validate_unique=False)
                        saved_event, _ = Event.objects.get_or_create(
                            submission_id=submission_id,
                            created_by=request.user,
                            workspace=workspace,
                            defaults={
                                "title": event_details.title,
                                "starts_at": event_details.starts_at,
                                "ends_at": event_details.ends_at,
                                "location": event_details.location,
                                "timezone_name": event_details.timezone_name,
                            },
                        )
                except ValidationError as error:
                    form.add_error(None, error.messages)
                except DatabaseError:
                    form.add_error(None, "The event could not be saved. Your details are still here. Please try again.")
                if not form.errors:
                    messages.success(request, "Your event has been saved.")
                    detail_url = reverse("events:detail", args=[saved_event.pk])
                    return redirect(f"{detail_url}?{urlencode({'timezone': timezone_name})}")

    return render(
        request,
        "events/create_event.html",
        {
            "form": form,
            "event_details": event_details,
            "timezone_name": timezone_name,
            "can_save": can_save,
            "workspace": workspace,
            "create_url": (
                reverse("events:workspace_create", args=[workspace.pk])
                if workspace else reverse("events:create")
            ),
        },
    )


@login_required(login_url="admin:login")
@require_GET
def choose_workspace(request):
    form = EventWorkspaceForm(
        request.GET if "workspace" in request.GET else None,
        user=request.user,
        initial={
            "date": request.GET.get("date"),
            "timezone": request.GET.get("timezone") or timezone.get_current_timezone_name(),
        },
    )
    if form.is_bound and form.is_valid():
        workspace = form.cleaned_data["workspace"]
        query = {"timezone": form.cleaned_data["timezone"]}
        if form.cleaned_data["date"]:
            query["date"] = form.cleaned_data["date"]
        return redirect(f"{reverse('events:workspace_create', args=[workspace.pk])}?{urlencode(query)}")
    return render(request, "events/choose_workspace.html", {"form": form})


@login_required(login_url="admin:login")
@require_GET
def event_detail(request, pk):
    event = get_object_or_404(
        Event.objects.select_related("workspace").filter(
            Q(workspace__primary_admin=request.user)
            | Q(workspace__isnull=True, created_by=request.user)
        ),
        pk=pk,
    )
    list_url = (
        reverse("events:workspace_list", args=[event.workspace_id])
        if event.workspace_id else reverse("events:list")
    )
    timezone_data = request.GET.copy()
    if not timezone_data.get("timezone"):
        timezone_data["timezone"] = event.timezone_name
    timezone_form = EventTimezoneForm(timezone_data)
    timezone_name = event.timezone_name
    if timezone_form.is_valid():
        timezone_name = timezone_form.cleaned_data["timezone"]
    schedule_query = urlencode({
        "date": timezone.localtime(event.starts_at, ZoneInfo(timezone_name)).date(),
        "view": "day",
        "timezone": timezone_name,
    })
    return render(
        request,
        "events/event_detail.html",
        {
            "event": event,
            "schedule_query": schedule_query,
            "list_url": list_url,
            "timezone_form": timezone_form,
            "timezone_name": timezone_name,
        },
    )


@login_required(login_url="admin:login")
@require_GET
def event_list(request, workspace_id=None):
    workspace = None
    if workspace_id is not None:
        workspace = get_object_or_404(Workspace, pk=workspace_id, primary_admin=request.user)
    if workspace:
        accessible_events = Event.objects.filter(workspace=workspace)
    else:
        accessible_events = Event.objects.filter(
            Q(workspace__primary_admin=request.user)
            | Q(workspace__isnull=True, created_by=request.user)
        )
    form = EventScheduleForm(request.GET)
    events = Event.objects.none()
    context = {"form": form, "workspace": workspace, "timezone_name": timezone.get_current_timezone_name()}
    if form.is_valid():
        period = form.cleaned_data
        events = accessible_events.filter(ends_at__gt=period["starts_at"])
        if period["view"] != "upcoming":
            events = events.filter(starts_at__lt=period["ends_at"])
        events = events.select_related("workspace").order_by("starts_at", "pk")
        timezone_query = {"timezone": period["timezone"]}
        create_query = urlencode({"date": period["date"], **timezone_query})
        context.update(
            timezone_name=period["timezone"],
            timezone_query=urlencode(timezone_query),
            create_query=create_query,
            period_query=urlencode({"date": period["date"], "view": period["view"], **timezone_query}),
        )
        if period["view"] != "upcoming":
            context.update(
                start_date=period["start_date"],
                last_date=period["last_date"],
                previous_query=urlencode({"date": period["previous_date"], "view": period["view"], **timezone_query}),
                next_query=urlencode({"date": period["next_date"], "view": period["view"], **timezone_query}),
            )
    page = Paginator(events, 20).get_page(request.GET.get("page"))
    context["page"] = page
    return render(request, "events/event_list.html", context)
