from django.contrib.admin.apps import AdminConfig


class SyncmilyAdminConfig(AdminConfig):
    """The Django admin, served by the two-step site in accounts/admin_site.py."""

    default_site = "accounts.admin_site.TwoStepAdminSite"
