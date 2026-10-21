
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

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
            "messages": [],
        },
    )


@login_required
def workspace_chat(request, workspace_id):
    workspace = get_object_or_404(
        accessible_workspaces(request.user),
        pk=workspace_id,
    )

    messages = get_household_messages(
        user=request.user,
        workspace_id=workspace.id,
        limit=50,
    )

    workspaces = accessible_workspaces(request.user).order_by("name")

    return render(
        request,
        "chat/chat_home.html",
        {
            "workspace": workspace,
            "workspaces": workspaces,
            "messages": messages,
        },
    )
