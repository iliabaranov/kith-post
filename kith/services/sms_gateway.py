"""Send SMS through the capcom6 SMS Gateway for Android.

The recommended primary for occasional, small-list sending: it sends from a SIM
the operator already owns, so there is no per-message fee, no number to rent and
no A2P registration. What it costs instead is a phone that has to stay powered
and reachable, and the same caveat WhatsApp carries — a consumer number sending
a hundred texts in an evening is what carriers throttle and filter. Keep lists
small.

Two deployment shapes, verified against the project's docs (docs.sms-gate.app):

* **Direct to device.** The app's on-device Local Server, reached at
  ``http://<phone-ip>:8080/message``. Basic auth only, with credentials the app
  shows in its Local Server section. Simplest for a home LAN, and the default here.
* **Self-hosted relay / cloud.** The server component, where the same call lives
  under ``/3rdparty/v1/messages``. Use it when the phone is not directly
  reachable from the kith container.

The two differ only in that path, so it is a setting with both values named as
constants below — a docs change is then a one-line fix rather than a hunt.

Either way this is LAN or compose-network traffic. It must never be published to
the public tunnel: the gateway's whole security model is that it isn't reachable.
"""

from __future__ import annotations

import httpx

from kith.services.sms import (
    SmsAuthError,
    SmsCaps,
    SmsError,
    SmsResult,
    SmsTimeout,
)

# The send path, per deployment shape. Both take the same JSON body and answer
# with the same {"id", "state"} object.
LOCAL_SERVER_PATH = "/message"                    # the app's on-device server
RELAY_PATH = "/3rdparty/v1/messages"              # the server component / cloud

# Response states that mean the message is on its way. Anything else in a 2xx
# answer is a refusal wearing a success code — see send().
ACCEPTED_STATES = frozenset({"Pending", "Sent", "Delivered", "Processed"})


class AndroidGatewayProvider:
    """One send per call, Basic auth, bounded timeout."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        *,
        device_id: str = "",
        path: str = LOCAL_SERVER_PATH,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = (base_url or "").rstrip("/") + (path or LOCAL_SERVER_PATH)
        self._auth = (user, password)
        self._device_id = (device_id or "").strip()
        # A short connect timeout separates "the phone is asleep or off the
        # network" — the common case, and worth failing fast on — from "it is
        # taking its time sending".
        self._timeout = httpx.Timeout(timeout, connect=5.0)
        # Tests inject an httpx.MockTransport here; production leaves it None.
        self._transport = transport

    def send(self, to_e164: str, text: str) -> SmsResult:
        # One recipient per call even though phoneNumbers is a list: the send
        # path paces and commits per recipient, and a batch would make one
        # failure ambiguous across several people.
        payload: dict = {"textMessage": {"text": text}, "phoneNumbers": [to_e164]}
        if self._device_id:
            # Only meaningful when a relay fronts several phones.
            payload["deviceId"] = self._device_id
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                resp = client.post(self._url, json=payload, auth=self._auth)
        except httpx.TimeoutException as e:
            raise SmsTimeout(f"gateway send to {to_e164} timed out") from e
        except httpx.HTTPError as e:
            raise SmsError(f"gateway send failed: {e}") from e

        if resp.status_code in (401, 403):
            raise SmsAuthError("the gateway rejected the credentials")
        if resp.status_code == 404:
            # Far and away the likeliest misconfiguration: a relay URL with the
            # on-device path, or the reverse. Say so rather than leaving the
            # operator to guess at a bare 404.
            raise SmsError(
                f"gateway 404 at {self._url} — check KITH_SMS_GATEWAY_PATH "
                f"({LOCAL_SERVER_PATH} for the on-device Local Server, "
                f"{RELAY_PATH} for a self-hosted relay)"
            )
        if resp.status_code >= 400:
            raise SmsError(f"gateway {resp.status_code}: {resp.text[:200]}")

        body = _json_or_empty(resp)
        state = body.get("state")
        # Like Twilio, the gateway can accept the request and refuse the
        # message. Reporting that as sent would flip the recipient to 'sent'
        # for a text that never went, and nothing retries a 'sent' row.
        if state is not None and state not in ACCEPTED_STATES:
            raise SmsError(f"gateway refused the message: state={state}")
        msg_id = body.get("id")
        return SmsResult(message_id=str(msg_id) if msg_id else None)

    def capabilities(self) -> SmsCaps:
        # The gateway posts both message-status and inbound-SMS webhooks, but
        # nothing receives them until the webhook endpoint exists.
        return SmsCaps(can_receipt=True, can_inbound=True)


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
