"""Talk to a self-hosted WAHA container (the WhatsApp channel's transport).

WAHA is an unofficial WhatsApp client we run ourselves, one session per kith
user, reached over the compose network with an ``X-Api-Key``. It holds the
WhatsApp credentials in its own volume; kith stores only the session name and
the last status we saw.

Two hard-won rules shape this module:

1. **Everything is bounded.** WAHA does *not* fail fast when a session isn't
   paired — ``sendText`` and ``contacts/check-exists`` were observed hanging
   indefinitely against a ``SCAN_QR_CODE``/``FAILED`` session, and
   ``sessions/{s}/timelock`` blocked for minutes before returning a gRPC 500.
   Every call therefore carries an explicit timeout, and the send path checks
   :meth:`SessionState.can_send` before it ever opens a socket.
2. **Read the guard rails off the session, not off their own endpoints.**
   ``GET /api/sessions/{s}`` already carries ``me.reachoutTimelock`` and
   ``me.messageCapping``. The dedicated ``/timelock`` and ``/capping`` endpoints
   force a fresh (blocking) fetch from WhatsApp, so they're exposed here only as
   an explicit, user-triggered refresh.

Verified against ``devlikeapro/waha:gows-2026.8.1`` (engine GOWS, tier CORE).
Payload shapes differ between engines, so switching engines means re-testing —
and, because the store is per-engine (``/app/.sessions/<engine>/<name>``),
re-pairing every session.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from kith.config import Settings
from kith.core import phones

log = logging.getLogger("kith")

# WAHA's session lifecycle. 2026.8.1 added the two PASSKEY_* states alongside
# the older QR flow, so the linking UI can't assume "scan a QR code".
STATUS_STOPPED = "STOPPED"
STATUS_STARTING = "STARTING"
STATUS_SCAN_QR = "SCAN_QR_CODE"
STATUS_PASSKEY = "PASSKEY_REQUIRED"
STATUS_PASSKEY_CONFIRM = "PASSKEY_CONFIRMATION_REQUIRED"
STATUS_WORKING = "WORKING"
STATUS_FAILED = "FAILED"

# Statuses where the host still has something to do to finish linking.
PAIRING_STATUSES = frozenset({STATUS_SCAN_QR, STATUS_PASSKEY, STATUS_PASSKEY_CONFIRM})


class WahaError(Exception):
    """WAHA couldn't be reached, or answered with something we can't use."""


class WahaAuthError(WahaError):
    """WAHA rejected our API key (401/403) — the deployment is misconfigured."""


class WahaNotFound(WahaError):
    """No such session in WAHA (404). The user needs to link again."""


class WahaTimeout(WahaError):
    """WAHA didn't answer in time — usually a session that isn't really working."""


class NotLinked(WahaError):
    """The user's session isn't WORKING, so nothing can be sent through it."""


class Timelocked(WahaError):
    """WhatsApp has restricted this account's reachout (the error-463 timelock).

    Stop sending and wait for ``ends_at``. Do NOT restart or re-pair the session:
    the restriction follows the WhatsApp account, not the WAHA session, and
    re-pairing only adds churn that looks worse to WhatsApp.
    """

    def __init__(self, ends_at: datetime | None = None) -> None:
        super().__init__("WhatsApp reachout timelock is active")
        self.ends_at = ends_at


class Capped(WahaError):
    """The account has spent its new-chat quota for this cycle."""

    def __init__(self, cycle_end: datetime | None = None) -> None:
        super().__init__("WhatsApp new-chat quota is exhausted")
        self.cycle_end = cycle_end


def _ts(value: Any) -> datetime | None:
    """A unix timestamp (seconds) from WAHA -> aware UTC datetime."""
    if not isinstance(value, int | float) or value <= 0:
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


@dataclass(frozen=True)
class Timelock:
    """``me.reachoutTimelock`` — WhatsApp's restriction on messaging new people."""

    is_active: bool
    ends_at: datetime | None
    enforcement_type: str | None

    @classmethod
    def parse(cls, data: Any) -> Timelock | None:
        if not isinstance(data, dict):
            return None
        return cls(
            is_active=bool(data.get("isActive")),
            ends_at=_ts(data.get("timeEnforcementEnds")),
            enforcement_type=data.get("enforcementType"),
        )


@dataclass(frozen=True)
class Capping:
    """``me.messageCapping`` — the per-cycle new-chat quota."""

    status: str
    total: int | None
    used: int | None
    cycle_end: datetime | None

    @property
    def is_capped(self) -> bool:
        return self.status == "CAPPED"

    @property
    def warning(self) -> bool:
        """Close to the cap — worth telling the host before a big send."""
        return self.status in {"FIRST_WARNING", "SECOND_WARNING"}

    @property
    def remaining(self) -> int | None:
        """New chats left this cycle; None when uncapped or unknown."""
        if self.total is None or self.used is None or self.total < 0:
            return None
        return max(0, self.total - self.used)

    @classmethod
    def parse(cls, data: Any) -> Capping | None:
        if not isinstance(data, dict):
            return None
        total = data.get("totalQuota")
        used = data.get("usedQuota")
        return cls(
            # WhatsApp may add values, so this stays an open set of strings.
            status=str(data.get("cappingStatus") or "NONE"),
            total=int(total) if isinstance(total, int | float) else None,
            used=int(used) if isinstance(used, int | float) else None,
            cycle_end=_ts(data.get("cycleEnd")),
        )


@dataclass(frozen=True)
class NumberCheck:
    """``contacts/check-exists`` — is this number reachable, and under which id."""

    exists: bool
    chat_id: str | None


@dataclass(frozen=True)
class SessionState:
    """What kith needs to know about one user's WAHA session."""

    name: str
    status: str
    phone: str | None = None       # the linked WhatsApp number, E.164
    push_name: str | None = None   # the account's display name
    timelock: Timelock | None = None
    capping: Capping | None = None

    @property
    def is_working(self) -> bool:
        return self.status == STATUS_WORKING

    @property
    def is_pairing(self) -> bool:
        return self.status in PAIRING_STATUSES

    @property
    def can_send(self) -> bool:
        """Safe to push a message through: linked, unrestricted, not capped."""
        if not self.is_working:
            return False
        if self.timelock is not None and self.timelock.is_active:
            return False
        return not (self.capping is not None and self.capping.is_capped)

    def raise_if_unsendable(self) -> None:
        """The send path's pre-flight: turn an unusable session into an error."""
        if self.timelock is not None and self.timelock.is_active:
            raise Timelocked(self.timelock.ends_at)
        if not self.is_working:
            raise NotLinked(f"session {self.name} is {self.status}")
        if self.capping is not None and self.capping.is_capped:
            raise Capped(self.capping.cycle_end)

    @classmethod
    def parse(cls, data: dict) -> SessionState:
        me = data.get("me") or {}
        wa_id = me.get("id") or ""
        return cls(
            name=str(data.get("name") or ""),
            status=str(data.get("status") or ""),
            phone=phones.from_chat_id(wa_id) if wa_id else None,
            push_name=me.get("pushName"),
            timelock=Timelock.parse(me.get("reachoutTimelock")),
            capping=Capping.parse(me.get("messageCapping")),
        )


class WahaClient:
    """A small, synchronous WAHA client. One instance per request is fine."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 20.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._key = api_key
        # A short connect timeout separates "WAHA is down" (fail fast, the
        # container isn't there) from "this session is wedged" (needs the full
        # read budget before we give up on it).
        self._timeout = httpx.Timeout(timeout, connect=5.0)
        # Tests inject an httpx.MockTransport here; production leaves it None.
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Settings) -> WahaClient:
        return cls(settings.waha_url, settings.waha_api_key, settings.waha_timeout_seconds)

    # --- plumbing ---------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        limit = self._timeout if timeout is None else httpx.Timeout(timeout, connect=5.0)
        try:
            with httpx.Client(transport=self._transport, timeout=limit) as client:
                resp = client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers={"X-Api-Key": self._key},
                )
        except httpx.TimeoutException as e:
            raise WahaTimeout(f"{method} {path} timed out") from e
        except httpx.HTTPError as e:
            raise WahaError(f"{method} {path} failed: {e}") from e
        if resp.status_code in (401, 403):
            raise WahaAuthError("WAHA rejected the API key")
        if resp.status_code == 404:
            raise WahaNotFound(f"{method} {path} -> 404")
        if resp.status_code == 422:
            # WAHA's clean answer to "this session isn't WORKING":
            #   {"error": ..., "status": "SCAN_QR_CODE", "expected": ["WORKING"]}
            # Worth distinguishing from a generic failure — it means the host has
            # to re-link, not that anything went wrong with the send. (It is not
            # a substitute for the pre-flight: a session wedged mid-transition
            # hangs instead of answering, which is what the timeout is for.)
            body = resp.json() if resp.headers.get("content-type", "").startswith(
                "application/json"
            ) else {}
            if isinstance(body, dict) and body.get("expected"):
                raise NotLinked(
                    f"session is {body.get('status')}, WAHA expected "
                    f"{body.get('expected')}"
                )
        if resp.status_code >= 400:
            raise WahaError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    def _json(self, *args, **kwargs) -> Any:  # noqa: ANN002, ANN003 — passthrough
        resp = self._call(*args, **kwargs)
        try:
            return resp.json()
        except ValueError as e:
            raise WahaError(f"WAHA returned non-JSON: {resp.text[:200]}") from e

    # --- server -----------------------------------------------------------

    def version(self) -> dict:
        """``{version, engine, tier, ...}`` — also our reachability check."""
        return self._json("GET", "/api/version", timeout=5.0)

    def healthy(self) -> bool:
        try:
            return bool(self.version().get("version"))
        except WahaError:
            return False

    # --- sessions ---------------------------------------------------------

    def get_session(self, name: str) -> SessionState:
        return SessionState.parse(self._json("GET", f"/api/sessions/{name}"))

    def find_session(self, name: str) -> SessionState | None:
        """:meth:`get_session`, but a missing session is None rather than an error."""
        try:
            return self.get_session(name)
        except WahaNotFound:
            return None

    def create_session(self, name: str, *, start: bool = True) -> SessionState:
        return SessionState.parse(
            self._json("POST", "/api/sessions", json={"name": name, "start": start})
        )

    def ensure_session(self, name: str) -> SessionState:
        """Get this session ready for the host to pair with.

        Idempotent, so the linking page can call it on every attempt without
        caring whether a previous try got half-way.

        The three cases are genuinely different verbs, and getting this wrong
        strands the host:

        * **missing** -> create it;
        * **STOPPED** -> ``start``, which is what start is for;
        * **FAILED** -> ``restart``. ``start`` is a *no-op* here — WAHA answers
          201 and logs "Session is already running", because the session object
          really is running; it's the WhatsApp connection underneath that died
          (typically a pairing window that expired unscanned). Only a restart
          brings it back to SCAN_QR_CODE.

        A session already waiting to pair is left alone: restarting it would
        throw away a QR the host is scanning or a code they're typing.
        """
        state = self.find_session(name)
        if state is None:
            return self.create_session(name, start=True)
        if state.status == STATUS_FAILED:
            return self.restart_session(name)
        if state.status == STATUS_STOPPED:
            return self.start_session(name)
        return state

    def start_session(self, name: str) -> SessionState:
        return SessionState.parse(self._json("POST", f"/api/sessions/{name}/start"))

    def restart_session(self, name: str) -> SessionState:
        """Tear the WhatsApp connection down and bring it back up.

        The only way out of FAILED (see :meth:`ensure_session`).
        """
        return SessionState.parse(self._json("POST", f"/api/sessions/{name}/restart"))

    def stop_session(self, name: str) -> None:
        self._call("POST", f"/api/sessions/{name}/stop")

    def logout_session(self, name: str) -> None:
        """Drop the WhatsApp pairing but keep the (now empty) session."""
        self._call("POST", f"/api/sessions/{name}/logout")

    def delete_session(self, name: str) -> None:
        """Remove the session and its stored credentials from WAHA's volume."""
        self._call("DELETE", f"/api/sessions/{name}")

    def unlink(self, name: str) -> None:
        """Best-effort full teardown: log out, then delete.

        Used by "unlink" and by account deletion, where leaving a paired session
        behind in WAHA's volume would outlive the account it belongs to. A
        session WAHA no longer has is already unlinked, so 404s are fine.
        """
        for step in (self.logout_session, self.delete_session):
            try:
                step(name)
            except WahaNotFound:
                return
            except WahaError:
                log.exception("waha: %s failed for session %s", step.__name__, name)

    # --- pairing ----------------------------------------------------------

    def qr_png(self, name: str) -> bytes:
        """The pairing QR as a PNG (~5 KB). Proxied by kith so the API key
        never has to reach the browser."""
        resp = self._call("GET", f"/api/{name}/auth/qr", params={"format": "image"})
        return resp.content

    def request_pairing_code(self, name: str, phone_e164: str) -> str:
        """Ask WhatsApp for a pairing code for this number, e.g. "WW5J-87T4".

        This is WhatsApp's "link with phone number instead" path, and it's the
        answer to the obvious hole in QR pairing: the code has to be scanned *by*
        the phone, so it can't be displayed *on* the phone. The host types this
        code into WhatsApp instead, and never needs a second screen.

        ``method`` is deliberately omitted — left empty it means web pairing (a
        code to type), rather than asking WhatsApp to send an SMS or call.
        """
        data = self._json(
            "POST",
            f"/api/{name}/auth/request-code",
            json={"phoneNumber": phones.digits(phone_e164)},
        )
        code = (data or {}).get("code") if isinstance(data, dict) else None
        if not code:
            raise WahaError("WhatsApp did not return a pairing code")
        return str(code)

    def qr_raw(self, name: str) -> str:
        """The pairing payload as text (a ``wa.me/settings/linked_devices#...``
        link), for a copy-paste fallback when the image won't render."""
        data = self._json("GET", f"/api/{name}/auth/qr", params={"format": "raw"})
        return str(data.get("value") or "") if isinstance(data, dict) else ""

    # --- guard rails (explicit refresh; these block on WhatsApp) -----------

    def refresh_timelock(self, name: str) -> Timelock | None:
        return Timelock.parse(self._json("GET", f"/api/sessions/{name}/timelock"))

    def refresh_capping(self, name: str) -> Capping | None:
        return Capping.parse(self._json("GET", f"/api/sessions/{name}/capping"))

    # --- messaging --------------------------------------------------------

    def check_exists(self, name: str, phone_e164: str) -> NumberCheck:
        """Is this number on WhatsApp? Only ever call this on a WORKING session.

        Worth doing before a send: messaging numbers that aren't really on
        WhatsApp is part of what earns an account a reachout timelock. The reply
        also carries the canonical chat id, which is not always ``<digits>@c.us``
        now that WhatsApp is moving accounts onto ``@lid`` identifiers.
        """
        data = self._json(
            "GET",
            "/api/contacts/check-exists",
            params={"phone": phones.digits(phone_e164), "session": name},
        )
        if not isinstance(data, dict):
            return NumberCheck(exists=False, chat_id=None)
        return NumberCheck(
            exists=bool(data.get("numberExists")),
            chat_id=data.get("chatId") or data.get("pn") or None,
        )

    # WhatsApp accepts far larger media, but the inline card copy is sized for
    # email (roughly 90-700KB), so anything past this is a sign something is
    # wrong rather than a card worth sending.
    MAX_IMAGE_BYTES = 8 * 1024 * 1024

    def send_image(
        self,
        name: str,
        phone_e164: str,
        image: bytes,
        *,
        mimetype: str = "image/jpeg",
        caption: str = "",
        filename: str = "card.jpg",
        chat_id: str | None = None,
        reply_to: str | None = None,
    ) -> dict:
        """Send the card itself, with the message as its caption.

        The image goes as base64 rather than as a URL: handing WhatsApp a link to
        the recipient's own invitation page would have Meta fetch that private
        page (again), whereas this keeps the picture on the compose network until
        the moment it is sent.
        """
        if len(image) > self.MAX_IMAGE_BYTES:
            raise WahaError(f"image is {len(image)} bytes, over the {self.MAX_IMAGE_BYTES} cap")
        body: dict = {
            "session": name,
            "chatId": chat_id or phones.chat_id(phone_e164),
            "file": {
                "mimetype": mimetype,
                "filename": filename,
                "data": base64.b64encode(image).decode(),
            },
            "caption": caption,
        }
        if reply_to:
            body["reply_to"] = reply_to
        return self._json("POST", "/api/sendImage", json=body)

    def send_text(
        self,
        name: str,
        phone_e164: str,
        text: str,
        *,
        link_preview: bool = True,
        chat_id: str | None = None,
    ) -> dict:
        """Send one message. Returns WAHA's result (carries the message ``id``).

        Pass ``chat_id`` to use an id resolved by :meth:`check_exists` instead of
        deriving it from the number.
        """
        return self._json(
            "POST",
            "/api/sendText",
            json={
                "session": name,
                "chatId": chat_id or phones.chat_id(phone_e164),
                "text": text,
                "linkPreview": link_preview,
            },
        )
