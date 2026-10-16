from django.urls import path

from . import views


app_name = "events"

urlpatterns = [
    path("new/", views.create_event, name="create"),
]
