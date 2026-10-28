from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from events.access import family_events
from events.templatetags.paint import initial, person
from events.timeline import family_members

from .access import accessible_workspaces
from .services import get_household_messages


@login_required
def chat_home(request):
    workspaces = accessible_workspaces(request.user).order_by("name")

    return render(
        request,
        "chat/chat_home.html",
        {
            "workspaces": workspaces,
            "workspace": None,
            "chat_messages": [],
        },
    )


@login_required
def workspace_chat(request, workspace_id):
    workspace = get_object_or_404(
        accessible_workspaces(request.user),
        pk=workspace_id,
    )

    chat_messages = get_household_messages(
        user=request.user,
        workspace_id=workspace.id,
        limit=50,
    )

    workspaces = accessible_workspaces(request.user).order_by("name")
    members = {member["user"].pk: member for member in family_members([workspace])}
    coming_up = list(
        family_events(request.user)
        .filter(workspace=workspace, ends_at__gt=timezone.now())
        .prefetch_related("participants")
        .order_by("starts_at", "pk")[:4]
    )
    for event in coming_up:
        event.people = [members.get(person_.pk, {"user": person_}) for person_ in event.participants.all()]

    return render(
        request,
        "chat/chat_home.html",
        {
            "workspace": workspace,
            "workspaces": workspaces,
            "chat_messages": chat_messages,
            "members": members,
            # Names and colours for messages that arrive over the socket (chat_home.js).
            "member_json": {
                str(pk): {
                    "name": person(member["user"]),
                    "initial": initial(member["user"]),
                    "hue": member["hue"],
                    "color": member["custom_color"] or "",
                }
                for pk, member in members.items()
            },
            "coming_up": coming_up,
        },
    )
