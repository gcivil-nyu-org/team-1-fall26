from axes.backends import AxesStandaloneBackend
from django.core.exceptions import PermissionDenied

from syncmily import limits


class LockoutBackend(AxesStandaloneBackend):
    """Axes lockout checks for every sign-in that comes through a web request.

    Logins without a request (the test client, shell scripts) have no IP to key a
    lockout on, so they skip the axes check instead of raising. Every login view
    passes the request, so visitors are always checked.

    On top of axes' (username, IP) lock, an account that drew 20 wrong passwords in
    an hour from any addresses pauses password sign-in for 15 minutes. The answer is
    the usual "wrong username or password", and password reset stays open.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        from accounts.services import sign_in_paused

        if username and sign_in_paused(username):
            # Stops every backend, so the right password fails too for now.
            raise PermissionDenied("Password sign-in is paused for this account.")
        if request is None:
            return None
        return super().authenticate(request, username, password, **kwargs)


def axes_username(request, credentials=None):
    """The username axes records: what the form sent, cut to the length a username
    can have, so a huge value is a normal failure and never a database error
    (H-FUZZ-04)."""
    value = None
    if credentials:
        value = credentials.get("username")
    if value is None and request is not None:
        value = request.POST.get("username")
    return str(value or "")[: limits.LOGIN_USERNAME_MAX]
