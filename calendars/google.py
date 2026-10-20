import os

import requests
from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

# Google returns scopes in its own order and lets people untick calendar access.
# Accept the token and check the granted scopes ourselves instead of failing.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", CALENDAR_SCOPE]
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _flow(redirect_uri, **kwargs):
    client_config = {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri, **kwargs)


def authorization_url(redirect_uri):
    """Return Google's consent URL, the state to check, and the PKCE code verifier."""
    flow = _flow(redirect_uri)
    # access_type=offline and prompt=consent make Google always send a refresh token.
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return url, state, flow.code_verifier


def exchange_code(redirect_uri, code, code_verifier):
    """Swap the authorization code for credentials and the verified account email."""
    flow = _flow(redirect_uri, code_verifier=code_verifier, autogenerate_code_verifier=False)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    claims = id_token.verify_oauth2_token(credentials.id_token, Request(), settings.GOOGLE_CLIENT_ID)
    return credentials, claims.get("email", "")


def revoke(token):
    """Ask Google to revoke a token. Return True when Google confirms."""
    try:
        response = requests.post(REVOKE_URL, data={"token": token}, timeout=10)
    except requests.RequestException:
        return False
    return response.ok
