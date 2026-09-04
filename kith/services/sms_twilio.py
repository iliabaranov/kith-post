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

import base64
import hashlib
import hmac
import logging

import httpx

from kith.services.sms import (
    SmsAuthError,
    SmsCaps,
    SmsError,
    SmsMisconfigured,
    SmsRateLimited,
    SmsResult,
    SmsTimeout,
)

log = logging.getLogger("kith")

# Pinned rather than assembled from a setting: Twilio's REST version is part of
# the contract this module was written against, not something to configure.
API_ROOT = "https://api.twilio.com/2010-04-01"

# A Messaging Service SID starts with "MG"; an Account SID with "AC". Checked
# before the first request so a from-number and a service SID swapped in the
# config stop the batch with a sentence, not an opaque 400 per recipient.
MESSAGING_SERVICE_PREFIX = "MG"

# Twilio error codes that describe *our* setup rather than this recipient, so
# every remaining recipient would fail identically: 20404 is "resource not
# found" (an account SID Twilio has never heard of); 21606 is a From number
# that isn't a Twilio number of ours, or can't send SMS. Codes about the To
# number (21211 invalid, 21610 opted out, 21408 region not enabled) are left to
# cost one recipient — the next guest may well be fine.
MISCONFIGURED_CODES = frozenset({20404, 21606})
# Twilio's own code for "too many requests", alongside the HTTP 429 it rides on.
RATE_LIMITED_CODE = 20429


# Twilio signs a webhook quite unlike the gateway does: base64 HMAC-SHA1, keyed
# with the account's auth token, over the full callback URL followed by every
# POST parameter sorted by name and concatenated as name+value with no
# separators. Verifying a Twilio callback with the gateway's body-HMAC (or the
# reverse) would reject every legitimate request, so the two live apart on
# purpose and each endpoint calls only its own.
TWILIO_SIGNATURE_HEADER = "x-twilio-signature"


def verify_twilio_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str | None
) -> bool:
    """Constant-time check that this POST really came from Twilio.

    ``url`` must be the exact URL Twilio was configured to call, including
    scheme, host and any query string — it is part of the signed material, so a
    proxy that rewrites it will make every callback fail to verify.
    """
    if not auth_token or not signature:
        return False
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature.strip())


# Twilio's message lifecycle, as a status callback reports it. "delivered" is
# the carrier confirming arrival; "sent" only means Twilio handed it off.
TWILIO_DELIVERED = frozenset({"delivered"})
TWILIO_FAILED = frozenset({"undelivered", "failed"})


class TwilioProvider:
    """One send per call, over HTTP Basic auth, with a bounded timeout."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        from_number: str = "",
        messaging_service_sid: str = "",
        status_callback: str = "",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sid = account_sid
        self._token = auth_token
        self._from = (from_number or "").strip()
        self._mss = (messaging_service_sid or "").strip()
        # Empty when receipts are off, in which case no callback is registered
        # and Twilio has nothing to post back to.
        self._status_callback = (status_callback or "").strip()
        # A short connect timeout separates "the internet is down" from "Twilio
        # is thinking about it", the same split services.waha makes.
        self._timeout = httpx.Timeout(timeout, connect=min(5.0, timeout))
        # Tests inject an httpx.MockTransport here; production leaves it None.
        self._transport = transport

    @property
    def _url(self) -> str:
        return f"{API_ROOT}/Accounts/{self._sid}/Messages.json"

    def send(self, to_e164: str, text: str) -> SmsResult:
        self._check_sender()
        data = {"To": to_e164, "Body": text}
        if self._mss:
            # A Messaging Service picks the sender itself, which is how you get
            # a sender pool or sticky sender. It wins when both are set: it is
            # the more specific instruction.
            data["MessagingServiceSid"] = self._mss
        else:
            data["From"] = self._from
        if self._status_callback:
            data["StatusCallback"] = self._status_callback
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                resp = client.post(self._url, data=data, auth=(self._sid, self._token))
        except httpx.TimeoutException as e:
            raise SmsTimeout(f"Twilio send to {to_e164} timed out") from e
        except httpx.HTTPError as e:
            raise SmsError(f"Twilio send failed: {e}") from e

        if resp.status_code in (401, 403):
            # Twilio's words are kept: 20003 (bad token) and 20005 (account
            # suspended) both land here, and they are not the same problem.
            raise SmsAuthError(f"Twilio rejected the credentials: {_error_detail(resp)}")
        code = _error_code(resp)
        if resp.status_code == 429 or code == RATE_LIMITED_CODE:
            raise SmsRateLimited(f"Twilio {resp.status_code}: {_error_detail(resp)}")
        if resp.status_code == 404 or code in MISCONFIGURED_CODES:
            # 404 on the Messages endpoint means the account SID in the URL is
            # not an account: an empty or mistyped KITH_SMS_TWILIO_ACCOUNT_SID.
            raise SmsMisconfigured(f"Twilio {resp.status_code}: {_error_detail(resp)}")
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

    def _check_sender(self) -> None:
        """Catch the two sender settings swapped, before spending a request on it."""
        if self._mss and not self._mss.startswith(MESSAGING_SERVICE_PREFIX):
            raise SmsMisconfigured(
                "KITH_SMS_TWILIO_MESSAGING_SERVICE_SID should start with "
                f"{MESSAGING_SERVICE_PREFIX!r} but starts {self._mss[:2]!r} — are the "
                "sender number and the service SID swapped?"
            )
        if not self._mss and not self._from.startswith("+"):
            raise SmsMisconfigured(
                "KITH_SMS_TWILIO_FROM should be a number in E.164 (starting '+'), "
                f"but is {self._from[:4]!r}… — set it, or set a Messaging Service SID"
            )

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


def _error_code(resp: httpx.Response) -> int | None:
    """Twilio's numeric error code from the body, when there is one."""
    code = _json_or_empty(resp).get("code")
    return code if isinstance(code, int) else None


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
