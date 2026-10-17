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
]