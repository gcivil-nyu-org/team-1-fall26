"""Navigation data for the app shell (top bar and phone tab bar) on every page."""

from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from events.access import family_workspaces
from events.timeline import family_members

from .models import Workspace
from .services import get_family_context


def _shell(user):
    family = get_family_context(user)
    workspace = family["workspace"] if family else None
    me = None
    if workspace:
        me = next((member for member in family_members([workspace]) if member["user"].pk == user.pk), None)
    can_create = bool(workspace) and family_workspaces(user, caregivers_only=True).filter(pk=workspace.pk).exists()
    return {
        "workspace": workspace,
        "me": me,
        "week_url": reverse("accounts:dashboard"),
        "day_url": (
            reverse("events:workspace_list", args=[workspace.pk]) if workspace else reverse("events:list")
        ) + "?view=day",
        # Straight into the family's thread; the chat home lists every family the user is in.
        "chat_url": reverse("chat:workspace_chat", args=[workspace.pk]) if workspace else reverse("chat:home"),
        # Only the primary admin has a family page today; everyone else sees the family on the week.
        "family_url": (
            reverse("accounts:workspace_detail", args=[workspace.pk])
            if family and family["is_primary_admin"] else reverse("accounts:dashboard")
        ),
        "add_event_url": reverse("events:workspace_create", args=[workspace.pk]) if can_create else None,
        "palette": workspace.palette if workspace else Workspace.Palette.PRIMARIES,
        # Parents choose the deck; kids see the family's choice.
        "palette_url": reverse("accounts:set_palette", args=[workspace.pk]) if can_create else None,
        "palettes": Workspace.Palette.choices,
    }


def shell(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"shell": None}
    # Lazy: pages that don't draw the shell (sign-in, health checks) never run these queries.
    return {"shell": SimpleLazyObject(lambda: _shell(user))}
