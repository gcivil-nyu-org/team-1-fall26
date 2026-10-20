from django.urls import path

from . import views


app_name = "calendars"

urlpatterns = [
    path("google/connect/<uuid:workspace_id>/", views.google_connect, name="google_connect"),
    path("google/callback/", views.google_callback, name="google_callback"),
    path("google/sync/", views.google_sync, name="google_sync"),
    path("google/disconnect/", views.google_disconnect, name="google_disconnect"),
]
