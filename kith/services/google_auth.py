"""Google OAuth web flow (login + consent to send-on-behalf).

We request the minimum sensitive scope (`gmail.send`) up front so the stored
refresh token can send from G3 onward. The actual network round-trip lives in
``exchange_code``; everything else is cheap to construct.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from kith.config import Settings

# Google adds `openid` to the granted scopes, which trips oauthlib's strict
# scope check; relax it so fetch_token doesn't raise on the benign difference.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
]


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str
    refresh_token: str | None


def _redirect_uri(s: Settings) -> str:
    return s.base_url.rstrip("/") + "/auth/callback"


def _client_config(s: Settings) -> dict:
    return {
        "web": {
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [_redirect_uri(s)],
        }
    }


def authorization_url(s: Settings) -> tuple[str, str, str | None]:
    """Return (auth_url, state, code_verifier).

    Google requires PKCE, so we keep the generated ``code_verifier`` and hand it
    back to the caller to stash in the session — ``exchange_code`` needs the *same*
    verifier, and it runs on a different request with a fresh Flow.
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(s),
        scopes=SCOPES,
        redirect_uri=_redirect_uri(s),
        autogenerate_code_verifier=True,
    )
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return url, state, flow.code_verifier


def exchange_code(
    s: Settings, code: str, state: str, code_verifier: str | None = None
) -> GoogleIdentity:
    """Exchange the auth code for tokens and verify the id_token claims."""
    import google.auth.transport.requests
    from google.oauth2 import id_token as google_id_token
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(s), scopes=SCOPES, state=state, redirect_uri=_redirect_uri(s)
    )
    flow.code_verifier = code_verifier  # the PKCE verifier from the auth step
    flow.fetch_token(code=code)
    creds = flow.credentials
    claims = google_id_token.verify_oauth2_token(
        creds.id_token, google.auth.transport.requests.Request(), s.google_client_id
    )
    return GoogleIdentity(
        sub=claims["sub"],
        email=claims.get("email", ""),
        name=claims.get("name", ""),
        refresh_token=creds.refresh_token,
    )
