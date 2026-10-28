
import logging
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.views import LoginView
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from calendars import google
from calendars.models import CalendarConnection
from events.access import family_events, family_workspaces
from events.timeline import editable_members, family_members
from events.week import build_today_rail, build_week, local_day_bounds, week_start_for

from . import two_step
from .forms import InviteMemberForm, SignUpForm, UsernameChangeForm, WorkspaceForm
from .models import GoogleIdentity, Membership, Workspace
from .services import (
    GoogleAccountTaken,
    GoogleEmailInUse,
    connect_google_account,
    accept_invitation,
    get_family_context,
    get_membership_from_token,
    invite_member,
    invite_url,
    invites_left,
    reissue_invite,
    send_invitation_email,
    user_for_google_account,
)

logger = logging.getLogger(__name__)

# Sign-ins that don't go through a password (sign-up, invites, Google) skip the
# lockout backend, which only guards password guesses.
DIRECT_LOGIN_BACKEND = "django.contrib.auth.backends.ModelBackend"
GOOGLE_LOGIN_SESSION_KEY = "google_login_oauth"
# How far the week view pages back or forward (about five years either way).
WEEK_RANGE_DAYS = 5 * 366


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

    calendar_connection = (
        request.user.calendar_connections.filter(provider=CalendarConnection.Provider.GOOGLE)
        .select_related("workspace")
        .first()
    )

    family = family_members([workspace])
    return render(
        request,
        "accounts/workspace_detail.html",
        {
            "workspace": workspace,
            "calendar_connection": calendar_connection,
            "family": family,
            "pending": workspace.memberships.filter(accepted_at__isnull=True).select_related("user"),
            "invite_form": InviteMemberForm(workspace=workspace),
            "color_members": editable_members(request.user, workspace, family),
        },
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
        family_events(request.user)
        .filter(workspace=workspace, ends_at__gt=timezone.now())
        .select_related("dropoff_driver", "pickup_driver")
        .order_by("starts_at", "pk")[:5]
    )
    members = workspace.memberships.select_related("user")

    context = {
        **family,
        "members": members,
        "upcoming_events": upcoming_events,
        "can_create_events": family_workspaces(
            request.user, caregivers_only=True,
        ).filter(pk=workspace.pk).exists(),
        "can_view_events": family_workspaces(request.user).filter(pk=workspace.pk).exists(),
    }
    context.update(_week_context(request, workspace))
    if family["role"] == Membership.Role.KID:
        context.update(_kid_context(request, context))
        return render(request, "accounts/kid_dashboard.html", context)
    return render(request, "accounts/parent_dashboard.html", context)


def _kid_context(request, context):
    """The kid's own row of the week, today and tomorrow, and the plans coming up that include them."""
    row = next((row for row in context["week"]["rows"] if row["user"].pk == request.user.pk), None)
    chips = {chip["date"]: chip for chip in row["chips"]} if row else {}
    today = context["today"]
    # Their own plans and whole-family plans, next three.
    upcoming = (
        family_events(request.user)
        .filter(workspace=context["workspace"], ends_at__gt=timezone.now())
        .filter(Q(participants=request.user) | Q(participants__isnull=True))
        .distinct()
        .order_by("starts_at", "pk")[:3]
    )
    return {
        "me_row": row,
        "my_week": row,
        "today_chip": chips.get(today),
        "tomorrow_chip": chips.get(today + timedelta(days=1)),
        "kid_upcoming": upcoming,
        "family": family_members([context["workspace"]]),
    }


def _week_context(request, workspace):
    """The paint-chip week for one family: ?week=YYYY-MM-DD picks the week, default this one."""
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    try:
        chosen = date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        chosen = today
    # Far-off dates (0001-01-01) would overflow the previous/next links; nobody plans decades out.
    if abs((chosen - today).days) > WEEK_RANGE_DAYS:
        chosen = today
    week_start = week_start_for(chosen)
    week_end = week_start + timedelta(days=7)
    starts_at, _ = local_day_bounds(week_start, tz)
    ends_at, _ = local_day_bounds(week_end, tz)
    events = list(
        family_events(request.user)
        .filter(workspace=workspace, starts_at__lt=ends_at, ends_at__gt=starts_at)
        .select_related("dropoff_driver", "pickup_driver")
        .prefetch_related("participants")
        .order_by("starts_at", "pk")
    )
    members = family_members([workspace])
    return {
        "week": build_week(events, members, week_start, tz, today),
        "week_start": week_start,
        "week_last": week_end - timedelta(days=1),
        "is_this_week": week_start == week_start_for(today),
        "previous_week": (week_start - timedelta(days=7)).isoformat(),
        "next_week": week_end.isoformat(),
        "today": today,
        "rail": build_today_rail(
            [event for event in events if event.starts_at.astimezone(tz).date() <= today
             and event.ends_at.astimezone(tz).date() >= today],
            members, today, tz, timezone.now(),
        ) if week_start == week_start_for(today) else None,
    }


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

        if form.is_valid() and not invites_left(request.user):
            form.add_error(
                None,
                "You've sent a lot of invites in the last hour. "
                "Try again a little later.",
            )
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
                report_invite(request, membership)
                return redirect(
                    "accounts:manage_members",
                    workspace_id=workspace.id,
                )
    else:
        form = InviteMemberForm(workspace=workspace)

    members = list(
        workspace.memberships
        .exclude(user_id=workspace.primary_admin_id)
        .select_related("user")
    )
    for member in members:
        if member.is_pending:
            member.invite_url = invite_url(request, member)

    return render(
        request,
        "accounts/manage_members.html",
        {
            "workspace": workspace,
            "form": form,
            "members": members,
            "family": family_members([workspace]),
            "pending": [member for member in members if member.is_pending],
        },
    )


def report_invite(request, membership):
    """
    Email the invite when the member has an address, and tell the admin what
    actually happened. The link stays on the members page either way.
    """
    username = membership.user.username
    if not membership.user.email:
        messages.success(
            request,
            f"{username} is ready. Copy their invite link below and send it "
            "to them.",
        )
        return
    try:
        send_invitation_email(request, membership)
    except OSError:
        messages.warning(
            request,
            f"The email to {membership.user.email} didn't go through. "
            f"Copy {username}'s invite link below and send it yourself.",
        )
    else:
        messages.success(
            request,
            f"Invite emailed to {membership.user.email}. "
            f"Their username is {username}.",
        )


@login_required
@require_POST
def resend_invite(request, workspace_id, membership_id):
    """Start a fresh invite for a pending member; older links stop working."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if workspace.primary_admin_id != request.user.pk:
        raise PermissionDenied("Only the primary admin can manage members.")
    membership = get_object_or_404(
        workspace.memberships.select_related("user"),
        pk=membership_id,
        accepted_at__isnull=True,
    )
    if not invites_left(request.user):
        messages.warning(
            request,
            "You've sent a lot of invites in the last hour. "
            "Try again a little later.",
        )
    else:
        report_invite(request, reissue_invite(membership))
    return redirect("accounts:manage_members", workspace_id=workspace.id)


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

    form = SetPasswordForm(membership.user, request.POST or None)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            form.save()
            accept_invitation(membership)
        login(request, membership.user, backend=DIRECT_LOGIN_BACKEND)
        messages.success(
            request,
            f"You're in {membership.workspace.name}, signed in as "
            f"{membership.user.username}.",
        )
        return redirect("accounts:dashboard")

    return render(
        request,
        "accounts/accept_invite.html",
        {"membership": membership, "form": form},
    )


def google_sign_in_enabled():
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


@require_http_methods(["GET", "POST"])
def signup(request):
    """Let anyone create an account; they set up or join a family afterwards."""
    if request.user.is_authenticated:
        return redirect("accounts:dashboard")

    form = SignUpForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user, backend=DIRECT_LOGIN_BACKEND)
        messages.success(
            request,
            f"Welcome to Syncmily, {user.username}! Create your family workspace to get started.",
        )
        return redirect("accounts:dashboard")

    return render(
        request,
        "accounts/signup.html",
        {"form": form, "google_sign_in": google_sign_in_enabled()},
    )


class SignInView(LoginView):
    """The password sign-in, which hands off to the code step when two-step sign-in is on."""

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["username"].widget.attrs["spellcheck"] = "false"
        return form

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "google_sign_in": google_sign_in_enabled()}

    def form_valid(self, form):
        user = form.get_user()
        if two_step.is_enabled(user):
            return two_step.begin(self.request, user, user.backend, self.get_success_url())
        return super().form_valid(form)


def _google_login_callback_uri(request):
    return request.build_absolute_uri(reverse("accounts:google_login_callback"))


def _start_google_sign_in(request, **pending):
    url, state, code_verifier = google.login_authorization_url(
        _google_login_callback_uri(request)
    )
    request.session[GOOGLE_LOGIN_SESSION_KEY] = {
        "state": state,
        "code_verifier": code_verifier,
        **pending,
    }
    return redirect(url)


@require_POST
def google_login(request):
    """Send the visitor to Google to choose the account they sign in with."""
    if not google_sign_in_enabled():
        messages.error(request, "Google sign-in isn’t set up on this server yet.")
        return redirect("accounts:login")

    return _start_google_sign_in(request)


@login_required
@require_POST
def google_connect(request):
    """Let a signed-in user add Google as a way to sign in to this account."""
    if not google_sign_in_enabled():
        messages.error(request, "Google sign-in isn’t set up on this server yet.")
        return redirect("accounts:profile")

    return _start_google_sign_in(request, connect_user_id=request.user.pk)


@login_required
@require_POST
def google_disconnect(request):
    if not request.user.has_usable_password():
        messages.error(request, "Set a password first, so you can still sign in without Google.")
        return redirect("accounts:profile")

    GoogleIdentity.objects.filter(user=request.user).delete()
    messages.success(request, "Google sign-in is turned off for this account.")
    return redirect("accounts:profile")


@require_GET
def google_login_callback(request):
    pending = request.session.pop(GOOGLE_LOGIN_SESSION_KEY, None)
    if not pending or request.GET.get("state") != pending["state"]:
        messages.error(request, "We couldn’t confirm that sign-in with Google. Please try again.")
        return redirect("accounts:login")
    if "code" not in request.GET:
        messages.error(request, "Google sign-in was cancelled.")
        return redirect("accounts:login")

    try:
        claims = google.verify_login(
            _google_login_callback_uri(request),
            request.GET["code"],
            pending["code_verifier"],
        )
    except Exception:
        logger.exception("Google sign-in failed")
        messages.error(request, "Google couldn’t sign you in. Please try again.")
        return redirect("accounts:login")

    connect_user_id = pending.get("connect_user_id")
    if connect_user_id is not None:
        # Started from the profile page: the password sign-in already proved who this is.
        if request.user.pk != connect_user_id:
            messages.error(request, "Sign in again to connect Google to your account.")
            return redirect("accounts:login")
        try:
            connect_google_account(request.user, claims["sub"], claims.get("email", ""))
        except GoogleAccountTaken:
            messages.error(request, "That Google account already signs in to a different Syncmily account.")
            return redirect("accounts:profile")
        messages.success(request, f"Done! You can now sign in with Google ({claims.get('email', '')}).")
        return redirect("accounts:profile")

    email = claims.get("email", "")
    if not email or not claims.get("email_verified"):
        messages.error(request, "Google hasn’t verified that account’s email, so we can’t use it to sign in.")
        return redirect("accounts:login")

    try:
        user = user_for_google_account(claims["sub"], email)
    except GoogleEmailInUse:
        messages.error(
            request,
            f"An account with {email} already exists. Sign in with its username and password, "
            "then connect Google from your profile.",
        )
        return redirect("accounts:login")

    if not user.is_active:
        messages.error(request, "This account isn’t active yet. Confirm your invitation first.")
        return redirect("accounts:login")

    if two_step.is_enabled(user):
        return two_step.begin(request, user, DIRECT_LOGIN_BACKEND)

    is_new = not user.last_login
    login(request, user, backend=DIRECT_LOGIN_BACKEND)
    if is_new:
        messages.success(
            request,
            f"Welcome to Syncmily, {user.username}! Create your family workspace to get started.",
        )
    return redirect("accounts:dashboard")


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    """
    Let any signed-in user change their username or password.
    """
    # Accounts made with Google have no password yet, so there's no current one to ask for.
    PasswordForm = PasswordChangeForm if request.user.has_usable_password() else SetPasswordForm
    username_form = UsernameChangeForm(instance=request.user)
    password_form = PasswordForm(request.user)
    action = request.POST.get("action")

    if request.method == "POST" and action == "username":
        username_form = UsernameChangeForm(request.POST, instance=request.user)

        if username_form.is_valid():
            username_form.save()
            messages.success(request, "Your username has been updated.")
            return redirect("accounts:profile")

    elif request.method == "POST" and action == "password":
        password_form = PasswordForm(request.user, request.POST)

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
            "google_identity": GoogleIdentity.objects.filter(user=request.user).first(),
            "google_sign_in": google_sign_in_enabled(),
            "two_step_on": two_step.is_enabled(request.user),
            "backup_codes_left": two_step.backup_codes_left(request.user),
        },
    )


@login_required
@require_POST
def set_palette(request, workspace_id):
    """A parent picks the paint deck their family's schedule is drawn in."""
    workspace = get_object_or_404(family_workspaces(request.user, caregivers_only=True), pk=workspace_id)
    palette = request.POST.get("palette", "")
    if palette not in Workspace.Palette.values:
        return HttpResponseBadRequest("Choose Saturday market, Morning garden or Crayon box.")
    if workspace.palette != palette:
        workspace.palette = palette
        workspace.save(update_fields=["palette"])
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
        next_url = reverse("accounts:dashboard")
    return redirect(next_url)

