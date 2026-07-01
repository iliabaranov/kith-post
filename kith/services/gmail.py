"""Send a prepared message via the Gmail API using the user's stored refresh
token. Only used in self-only / live modes (dry-run never reaches here)."""

from __future__ import annotations

from kith.config import Settings
from kith.services.google_auth import SCOPES


def gmail_send(
    settings: Settings, refresh_token: str, raw_b64: str, thread_id: str | None = None
) -> dict:
    """messages.send(raw). Returns the API result (has 'id' and 'threadId'). Pass
    thread_id to thread a reminder/re-send under the original message's Gmail thread."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    body = {"raw": raw_b64}
    if thread_id:
        body["threadId"] = thread_id
    return service.users().messages().send(userId="me", body=body).execute()
