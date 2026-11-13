from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import two_step, views

app_name = "accounts"

urlpatterns = [
    path(
        "workspaces/create/",
        views.create_workspace,
        name="create_workspace",
    ),
    path(
        "workspaces/<uuid:workspace_id>/",
        views.workspace_detail,
        name="workspace_detail",
    ),
    path(
        "workspaces/<uuid:workspace_id>/members/",
        views.manage_members,
        name="manage_members",
    ),
    path(
        "workspaces/<uuid:workspace_id>/members/<int:membership_id>/resend/",
        views.resend_invite,
        name="resend_invite",
    ),
    path(
        "workspaces/<uuid:workspace_id>/members/<int:membership_id>/remove/",
        views.remove_member,
        name="remove_member",
    ),
    path(
        "dashboard/",
        views.dashboard,
        name="dashboard",
    ),
    path(
        "dashboard/my-day/seen/",
        views.day_seen,
        name="day_seen",
    ),
    path(
        "profile/",
        views.profile,
        name="profile",
    ),
    path(
        "invite/<str:token>/",
        views.accept_invite,
        name="accept_invite",
    ),
    path(
        "signup/",
        views.signup,
        name="signup",
    ),
    path(
        "google/login/",
        views.google_login,
        name="google_login",
    ),
    path(
        "google/connect/",
        views.google_connect,
        name="google_connect",
    ),
    path(
        "google/disconnect/",
        views.google_disconnect,
        name="google_disconnect",
    ),
    path(
        "google/callback/",
        views.google_login_callback,
        name="google_login_callback",
    ),
    path(
        "workspaces/<uuid:workspace_id>/palette/", views.set_palette, name="set_palette"
    ),
    path(
        "workspaces/<uuid:workspace_id>/my-palette/",
        views.set_my_palette,
        name="set_my_palette",
    ),
    path("login/", views.SignInView.as_view(), name="login"),
    path("login/verify/", two_step.verify, name="two_step_verify"),
    path("two-step/setup/", two_step.setup, name="two_step_setup"),
    path("two-step/disable/", two_step.disable, name="two_step_disable"),
    path(
        "two-step/backup-codes/",
        two_step.new_backup_codes,
        name="two_step_backup_codes",
    ),
    path("password-reset/", views.ResetRequestView.as_view(), name="password_reset"),
    path(
        "password-reset/sent/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
]
