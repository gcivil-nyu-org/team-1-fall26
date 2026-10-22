from axes.backends import AxesStandaloneBackend


class LockoutBackend(AxesStandaloneBackend):
    """Axes lockout checks for every sign-in that comes through a web request.

    Logins without a request (the test client, shell scripts) have no IP to key a
    lockout on, so they skip the check instead of raising. Every login view passes
    the request, so visitors are always checked.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if request is None:
            return None
        return super().authenticate(request, username, password, **kwargs)
