from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

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
        "dashboard/",
        views.dashboard,
        name="dashboard",
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
        "login/",
        auth_views.LoginView.as_view(
            template_name="accounts/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),
]