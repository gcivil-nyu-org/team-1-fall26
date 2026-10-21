
from django.urls import path
from . import views

app_name = "chat"

urlpatterns = [
    path("", views.chat_home, name="home"),
    path(
        "<uuid:workspace_id>/",
        views.workspace_chat,
        name="workspace_chat",
    ),
]
