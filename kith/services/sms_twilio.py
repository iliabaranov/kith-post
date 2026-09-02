"""Send SMS through Twilio's REST API.

The reliable, terms-clean option: a real carrier relationship, global reach, and
delivery receipts that mean something. It costs per message (pennies at a party's
scale) plus two recurring fees that dominate at this volume — the number rental
and the A2P 10DLC campaign registration.

Deliberately NOT the `twilio` PyPI package. This module makes exactly one kind
of request; the SDK would add a dependency tree, its own retry and logging
behaviour, and a mocking story for the tests, in exchange for saving about
fifteen lines. `httpx` is already a dependency, and `services.waha` established
the shape: bounded timeout, injectable transport, provider errors mapped onto
one hierarchy.
"""

from __future__ import annotations

import logging

import httpx

from kith.services.sms import (
    SmsAuthError,
    SmsCaps,
    SmsError,
    SmsResult,
    SmsTimeout,
)

log = logging.getLogger("kith")

# Pinned rather than assembled from a setting: Twilio's REST version is part of
# the contract this module was written against, not something to configure.
API_ROOT = "https://api.twilio.com/2010-04-01"

# A Messaging Service SID starts with "MG"; an Account SID with "AC". Used only
# to catch a from-number and a service SID swapped in the config, which
# otherwise fails as an opaque 400 on the first real send.
MESSAGING_SERVICE_PREFIX = "MG"


class TwilioProvider:
    """One send per call, over HTTP Basic auth, with a bounded timeout."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        from_number: str = "",
        messaging_service_sid: str = "",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = (from_number or "").strip()
        self._mss = (messaging_service_sid or "").strip()
        # A short connect timeout separates "the internet is down" from "Twilio
        # is thinking about it", the same split services.waha makes.
        self._timeout = httpx.Timeout(timeout, connect=5.0)
        # Tests inject an httpx.MockTransport here; production leaves it None.
        self._transport = transport

    @property
    def _url(self) -> str:
        return f"{API_ROOT}/Accounts/{self._sid}/Messages.json"

    def send(self, to_e164: str, text: str) -> SmsResult:
        data = {"To": to_e164, "Body": text}
        if self._mss:
            # A Messaging Service picks the sender itself, which is how you get
            # a sender pool or sticky sender. It wins when both are set: it is
            # the more specific instruction.
            data["MessagingServiceSid"] = self._mss
        else:
            data["From"] = self._from
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                resp = client.post(self._url, data=data, auth=(self._sid, self._token))
        except httpx.TimeoutException as e:
            raise SmsTimeout(f"Twilio send to {to_e164} timed out") from e
        except httpx.HTTPError as e:
            raise SmsError(f"Twilio send failed: {e}") from e

        if resp.status_code in (401, 403):
            raise SmsAuthError("Twilio rejected the credentials")
        if resp.status_code >= 400:
            raise SmsError(f"Twilio {resp.status_code}: {_error_detail(resp)}")

        body = _json_or_empty(resp)
        sid = body.get("sid")
        # Twilio can answer 201 and still have refused the message, with the
        # reason in the body rather than the status. Treating that as a success
        # would flip the recipient to 'sent' for a text that never went.
        if body.get("error_code"):
            raise SmsError(
                f"Twilio accepted then rejected the message: "
                f"{body.get('error_code')} {body.get('error_message') or ''}".strip()
            )
        return SmsResult(message_id=str(sid) if sid else None)

    def capabilities(self) -> SmsCaps:
        # Twilio posts both, but nothing is wired to receive them yet: a
        # StatusCallback URL is only set once the webhook endpoint exists.
        return SmsCaps(can_receipt=True, can_inbound=True)


def _json_or_empty(resp: httpx.Response) -> dict:
    """Twilio's body, or {} — a malformed answer is not worth an exception here."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_detail(resp: httpx.Response) -> str:
    """Twilio's own words for a failure, which are usually actionable.

    An error body carries a numeric `code` and a `message` ("The 'To' number is
    not a valid phone number"). Falling back to the raw text keeps a proxy's
    HTML error page from being swallowed, capped so a stack trace of one doesn't
    end up in the logs.
    """
    body = _json_or_empty(resp)
    code, message = body.get("code"), body.get("message")
    if code or message:
        return f"{code or '?'} {message or ''}".strip()
    return resp.text[:200]
