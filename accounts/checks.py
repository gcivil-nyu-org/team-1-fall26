from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

# Backends that never deliver mail: they print it, keep it in memory or drop it.
NON_DELIVERING_BACKENDS = {
    "django.core.mail.backends.console.EmailBackend",
    "django.core.mail.backends.locmem.EmailBackend",
    "django.core.mail.backends.dummy.EmailBackend",
    "django.core.mail.backends.filebased.EmailBackend",
}


@register(Tags.security, deploy=True)
def check_email_delivery(app_configs, **kwargs):
    """
    Invites only work if their emails reach people. The console backend also
    writes every invite link into the server log, where anyone reading the
    logs could use it.
    """
    if settings.DEBUG:
        return []
    problems = []
    if settings.EMAIL_BACKEND in NON_DELIVERING_BACKENDS:
        problems.append(Error(
            f"EMAIL_BACKEND is {settings.EMAIL_BACKEND}, so invite emails are "
            "never delivered and their links end up in the server log.",
            hint="Set DJANGO_EMAIL_BACKEND to django.core.mail.backends.smtp.EmailBackend "
                 "and the EMAIL_HOST_* settings.",
            id="accounts.E001",
        ))
    if settings.DEFAULT_FROM_EMAIL.rstrip(">").endswith(".local"):
        problems.append(Warning(
            f"DEFAULT_FROM_EMAIL is {settings.DEFAULT_FROM_EMAIL}; mail from a "
            ".local address lands in spam or bounces.",
            hint="Set DJANGO_DEFAULT_FROM_EMAIL, e.g. Syncmily <no-reply@syncmily.app>.",
            id="accounts.W001",
        ))
    return problems
