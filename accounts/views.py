
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render

from .forms import WorkspaceForm
from .models import Workspace


@login_required
def create_workspace(request):
    """
    Allow an authenticated user to create a family workspace.
    The creator is automatically assigned as Primary Admin.
    """
    if request.method == "POST":
        form = WorkspaceForm(request.POST)

        if form.is_valid():
            try:
                with transaction.atomic():
                    workspace = form.save(commit=False)
                    workspace.primary_admin = request.user
                    workspace.save()

                return redirect(
                    "accounts:workspace_detail",
                    workspace_id=workspace.id,
                )

            except DatabaseError:
                form.add_error(
                    None,
                    "Workspace creation failed. Please try again.",
                )
    else:
        form = WorkspaceForm()

    return render(
        request,
        "accounts/create_workspace.html",
        {"form": form},
    )


@login_required
def workspace_detail(request, workspace_id):
    """
    Display a workspace only to its Primary Admin.
    """
    workspace = get_object_or_404(
        Workspace,
        id=workspace_id,
    )

    if workspace.primary_admin_id != request.user.pk:
        raise PermissionDenied(
            "You do not have permission to access this workspace."
        )

    return render(
        request,
        "accounts/workspace_detail.html",
        {"workspace": workspace},
    )
