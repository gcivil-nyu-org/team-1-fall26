from urllib.parse import urlencode
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .forms import EventForm, EventScheduleForm
from .models import Event


@require_http_methods(["GET", "POST"])
def create_event(request):
    can_save = request.user.is_authenticated and request.user.has_perm("events.add_event")
    saving = request.method == "POST" and request.POST.get("action") == "save"
    if saving and not can_save:
        raise PermissionDenied

    form = EventForm(
        request.POST if request.method == "POST" else None,
        user=request.user,
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
                if submission["user"] != request.user.pk:
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
                            defaults={
                                "title": event_details.title,
                                "starts_at": event_details.starts_at,
                                "ends_at": event_details.ends_at,
                                "location": event_details.location,
                            },
                        )
                except ValidationError as error:
                    form.add_error(None, error.messages)
                except IntegrityError:
                    form.add_error(None, "The event could not be saved. Please try again.")
                if not form.errors:
                    messages.success(request, "Your event has been saved.")
                    return redirect("events:detail", pk=saved_event.pk)

    return render(
        request,
        "events/create_event.html",
        {
            "form": form,
            "event_details": event_details,
            "timezone_name": timezone_name,
            "can_save": can_save,
        },
    )


@login_required(login_url="admin:login")
@require_GET
def event_detail(request, pk):
    event = get_object_or_404(Event, pk=pk, created_by=request.user)
    schedule_query = urlencode({"date": timezone.localtime(event.starts_at).date(), "view": "day"})
    return render(
        request,
        "events/event_detail.html",
        {"event": event, "schedule_query": schedule_query},
    )


@login_required(login_url="admin:login")
@require_GET
def event_list(request):
    form = EventScheduleForm(request.GET)
    events = Event.objects.none()
    context = {"form": form}
    if form.is_valid():
        period = form.cleaned_data
        events = Event.objects.filter(
            created_by=request.user,
            starts_at__lt=period["ends_at"],
            ends_at__gt=period["starts_at"],
        ).order_by("starts_at", "pk")
        context.update(
            start_date=period["start_date"],
            last_date=period["last_date"],
            previous_query=urlencode({"date": period["previous_date"], "view": period["view"]}),
            next_query=urlencode({"date": period["next_date"], "view": period["view"]}),
            period_query=urlencode({"date": period["date"], "view": period["view"]}),
        )
    page = Paginator(events, 20).get_page(request.GET.get("page"))
    context["page"] = page
    return render(request, "events/event_list.html", context)
