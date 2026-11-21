from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from syncmily import views

urlpatterns = [
    # The bare domain opens the dashboard (signed-out visitors land on the login page).
    path("", RedirectView.as_view(pattern_name="accounts:dashboard"), name="home"),
    path(settings.ADMIN_PATH, admin.site.urls),
    path("events/", include("events.urls")),
    path("chat/", include("chat.urls")),
    path("accounts/", include("accounts.urls")),
    path("calendars/", include("calendars.urls")),
    path("checklists/", include("checklists.urls")),
    # Unnamed on purpose: it is CSRF-exempt, so the named-route CSRF sweep skips it.
    path("csp-report/", views.csp_report),
]

handler400 = "syncmily.errors.handler400"
handler403 = "syncmily.errors.handler403"
handler404 = "syncmily.errors.handler404"
handler500 = "syncmily.errors.handler500"
