from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .forms import EventForm


@require_http_methods(["GET", "POST"])
def create_event(request):
    form = EventForm(request.POST if request.method == "POST" else None)
    event_details = None
    if request.method == "POST" and form.is_valid():
        event_details = form.cleaned_data

    return render(
        request,
        "events/create_event.html",
        {"form": form, "event_details": event_details},
    )
