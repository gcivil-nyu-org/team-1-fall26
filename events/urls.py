from django.urls import path

from . import views


app_name = "events"

urlpatterns = [
    path("", views.event_list, name="list"),
    path("choose-workspace/", views.choose_workspace, name="choose_workspace"),
    path("new/", views.create_event, name="create"),
    path("workspaces/<uuid:workspace_id>/", views.event_list, name="workspace_list"),
    path("workspaces/<uuid:workspace_id>/new/", views.create_event, name="workspace_create"),
    path("workspaces/<uuid:workspace_id>/colors/", views.member_colors, name="member_colors"),
    path("<int:pk>/", views.event_detail, name="detail"),
]
