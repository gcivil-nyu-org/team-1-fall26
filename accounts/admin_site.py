"""The Django admin with two-step sign-in (finding admin-login-no-mfa).

The stock admin login form signs staff in with the password alone, so a staff
member with two-step on skipped their code there. This site sends such sign-ins
through the same code step as the app (accounts/two_step.py), and only lets those
staff into the admin with a session that django-otp marks as verified.
"""

from django.contrib.admin import AdminSite
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.views import LoginView


class TwoStepAdminLoginView(LoginView):
    authentication_form = AdminAuthenticationForm
    template_name = "admin/login.html"

    def form_valid(self, form):
        from . import two_step

        user = form.get_user()
        if two_step.is_enabled(user):
            return two_step.begin(
                self.request, user, user.backend, self.get_success_url()
            )
        return super().form_valid(form)


class TwoStepAdminSite(AdminSite):
    def has_permission(self, request):
        from . import two_step

        if not super().has_permission(request):
            return False
        user = request.user
        if not two_step.is_enabled(user):
            return True
        is_verified = getattr(user, "is_verified", None)
        return bool(is_verified and is_verified())

    def login(self, request, extra_context=None):
        """AdminSite.login (Django 5.2), with the two-step login view."""
        if request.method == "GET" and self.has_permission(request):
            return HttpResponseRedirect(reverse("admin:index", current_app=self.name))
        context = {
            **self.each_context(request),
            "title": _("Log in"),
            "subtitle": None,
            "app_path": request.get_full_path(),
            "username": request.user.get_username(),
        }
        if (
            REDIRECT_FIELD_NAME not in request.GET
            and REDIRECT_FIELD_NAME not in request.POST
        ):
            context[REDIRECT_FIELD_NAME] = reverse("admin:index", current_app=self.name)
        context.update(extra_context or {})
        request.current_app = self.name
        return TwoStepAdminLoginView.as_view(
            extra_context=context,
            authentication_form=self.login_form or AdminAuthenticationForm,
            template_name=self.login_template or "admin/login.html",
        )(request)
