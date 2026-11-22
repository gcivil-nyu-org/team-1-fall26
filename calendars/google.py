import os

import requests
from django.conf import settings
from google.auth.transport import requests as google_transport
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

# Google returns scopes in its own order and lets people untick calendar access.
# Accept the token and check the granted scopes ourselves instead of failing.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", CALENDAR_SCOPE]
# google-auth waits 120 s for Google's signing certificates; nginx gives up at 60 s.
google_transport._DEFAULT_TIMEOUT = 10
# (connect, read) seconds for the token exchange.
TOKEN_TIMEOUT = (3.05, 10)
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


# Sign in with Google only needs to know who the person is.
LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]


class Request(google_transport.Request):
    """google-auth's transport, with the wait for Google's certificates capped."""

    def __call__(
        self, url, method="GET", body=None, headers=None, timeout=10, **kwargs
    ):
        return super().__call__(
            url, method, body, headers, timeout=min(timeout or 10, 10), **kwargs
        )


def _add_timeout(session):
    """Give every request the session makes a timeout: requests waits forever by
    default, and fetch_token sends timeout=None unless the caller sets one."""
    send = session.request

    def request(*args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = TOKEN_TIMEOUT
        return send(*args, **kwargs)

    session.request = request


def _flow(redirect_uri, scopes=SCOPES, **kwargs):
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(
        client_config, scopes=scopes, redirect_uri=redirect_uri, **kwargs
    )
    _add_timeout(flow.oauth2session)
    return flow


def authorization_url(redirect_uri):
    """Return Google's consent URL, the state to check, and the PKCE code verifier."""
    flow = _flow(redirect_uri)
    # access_type=offline and prompt=consent make Google always send a refresh token;
    # select_account shows the account picker, so people can switch Google accounts.
    url, state = flow.authorization_url(
        access_type="offline", prompt="select_account consent"
    )
    return url, state, flow.code_verifier


def exchange_code(redirect_uri, code, code_verifier):
    """Swap the authorization code for credentials and the verified account email."""
    flow = _flow(
        redirect_uri, code_verifier=code_verifier, autogenerate_code_verifier=False
    )
    flow.fetch_token(code=code)
    credentials = flow.credentials
    claims = id_token.verify_oauth2_token(
        credentials.id_token, Request(), settings.GOOGLE_CLIENT_ID
    )
    return credentials, claims.get("email", "")


def login_authorization_url(redirect_uri):
    """Return Google's sign-in URL, the state to check, and the PKCE code verifier."""
    flow = _flow(redirect_uri, scopes=LOGIN_SCOPES)
    url, state = flow.authorization_url(prompt="select_account")
    return url, state, flow.code_verifier


def verify_login(redirect_uri, code, code_verifier):
    """Swap the authorization code for Google's signed ID token and return its checked
    claims.
    """
    flow = _flow(
        redirect_uri,
        scopes=LOGIN_SCOPES,
        code_verifier=code_verifier,
        autogenerate_code_verifier=False,
    )
    flow.fetch_token(code=code)
    return id_token.verify_oauth2_token(
        flow.credentials.id_token, Request(), settings.GOOGLE_CLIENT_ID
    )


def revoke(token):
    """Ask Google to revoke a token. Return True when Google confirms."""
    if not token:
        return False
    try:
        response = requests.post(REVOKE_URL, data={"token": token}, timeout=10)
    except requests.RequestException:
        return False
    return response.ok
