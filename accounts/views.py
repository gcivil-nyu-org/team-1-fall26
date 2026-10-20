
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from events.models import Event

from .forms import InviteMemberForm, UsernameChangeForm, WorkspaceForm
from .models import Membership, Workspace
from .services import (
    DEFAULT_MEMBER_PASSWORD,
    accept_invitation,
    get_family_context,
    get_membership_from_token,
    invite_member,
    send_invitation_email,
)


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


@login_required
def dashboard(request):
    """
    Show the parent or kid dashboard for the user's family workspace.
    """
    family = get_family_context(request.user)

    if family is None:
        return redirect("accounts:create_workspace")

    workspace = family["workspace"]
    upcoming_events = (
        Event.objects
        .filter(workspace=workspace, ends_at__gt=timezone.now())
        .order_by("starts_at", "pk")[:5]
    )
    members = workspace.memberships.select_related("user")

    template = (
        "accounts/kid_dashboard.html"
        if family["role"] == Membership.Role.KID
        else "accounts/parent_dashboard.html"
    )

    return render(
        request,
        template,
        {
            **family,
            "members": members,
            "upcoming_events": upcoming_events,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def manage_members(request, workspace_id):
    """
    Let the Primary Admin invite parents and kids to their workspace.
    """
    workspace = get_object_or_404(
        Workspace,
        id=workspace_id,
    )

    if workspace.primary_admin_id != request.user.pk:
        raise PermissionDenied(
            "Only the primary admin can manage members."
        )

    if request.method == "POST":
        form = InviteMemberForm(request.POST, workspace=workspace)

        if form.is_valid():
            try:
                membership = invite_member(
                    workspace=workspace,
                    invited_by=request.user,
                    email=form.cleaned_data["email"],
                    role=form.cleaned_data["role"],
                )
            except DatabaseError:
                form.add_error(
                    None,
                    "The invitation could not be created. Please try again.",
                )
            else:
                try:
                    send_invitation_email(request, membership)
                except OSError:
                    messages.warning(
                        request,
                        f"{membership.user.username} was created, but the "
                        "invitation email could not be sent.",
                    )
                else:
                    messages.success(
                        request,
                        f"Invitation sent to {membership.user.email}. "
                        f"Their username is {membership.user.username}.",
                    )

                return redirect(
                    "accounts:manage_members",
                    workspace_id=workspace.id,
                )
    else:
        form = InviteMemberForm(workspace=workspace)

    return render(
        request,
        "accounts/manage_members.html",
        {
            "workspace": workspace,
            "form": form,
            "members": workspace.memberships.select_related("user"),
            "default_password": DEFAULT_MEMBER_PASSWORD,
        },
    )


@require_http_methods(["GET", "POST"])
def accept_invite(request, token):
    """
    Confirm an emailed invitation. Confirming happens on POST so that
    email link scanners cannot activate accounts by opening the link.
    """
    membership = get_membership_from_token(token)

    if membership is None:
        return render(
            request,
            "accounts/invite_invalid.html",
            status=400,
        )

    if not membership.is_pending:
        messages.info(
            request,
            "This invitation has already been confirmed. Please sign in.",
        )
        return redirect("accounts:login")

    if request.method == "POST":
        accept_invitation(membership)
        messages.success(
            request,
            f"You're in! Sign in as {membership.user.username} "
            "with the password from your invitation email.",
        )
        return redirect("accounts:login")

    return render(
        request,
        "accounts/accept_invite.html",
        {"membership": membership},
    )


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    """
    Let any signed-in user change their username or password.
    """
    username_form = UsernameChangeForm(instance=request.user)
    password_form = PasswordChangeForm(request.user)
    action = request.POST.get("action")

    if request.method == "POST" and action == "username":
        username_form = UsernameChangeForm(request.POST, instance=request.user)

        if username_form.is_valid():
            username_form.save()
            messages.success(request, "Your username has been updated.")
            return redirect("accounts:profile")

    elif request.method == "POST" and action == "password":
        password_form = PasswordChangeForm(request.user, request.POST)

        if password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Your password has been updated.")
            return redirect("accounts:profile")

    return render(
        request,
        "accounts/profile.html",
        {
            "username_form": username_form,
            "password_form": password_form,
            "family": get_family_context(request.user),
        },
    )
