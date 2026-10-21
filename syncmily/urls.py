
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    # The bare domain opens the dashboard (signed-out visitors land on the login page).
    path("", RedirectView.as_view(pattern_name="accounts:dashboard"), name="home"),
    path("admin/", admin.site.urls),
    path("events/", include("events.urls")),
    path("chat/", include("chat.urls")),
    path("accounts/", include("accounts.urls")),
    path("calendars/", include("calendars.urls")),
]
