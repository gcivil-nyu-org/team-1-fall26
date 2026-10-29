from django.shortcuts import get_object_or_404

from events.access import family_workspaces

from .models import Checklist


def accessible_workspaces(user):
    """Workspaces the user is authorized to view."""
    return family_workspaces(user)


def caregiver_workspaces(user):
    """Workspaces where the user has caregiver permissions."""
    return family_workspaces(user, caregivers_only=True)


def get_viewable_workspace(user, workspace_id):
    return get_object_or_404(
        accessible_workspaces(user),
        pk=workspace_id,
    )


def get_manageable_workspace(user, workspace_id):
    return get_object_or_404(
        caregiver_workspaces(user),
        pk=workspace_id,
    )


def get_viewable_checklist(user, checklist_id):
    return get_object_or_404(
        Checklist.objects.filter(workspace__in=accessible_workspaces(user)),
        pk=checklist_id,
    )


def get_manageable_checklist(user, checklist_id):
    return get_object_or_404(
        Checklist.objects.filter(workspace__in=caregiver_workspaces(user)),
        pk=checklist_id,
    )
