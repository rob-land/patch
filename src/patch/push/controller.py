"""Glue between the distributor, the connector, the XMPP stream, and the
account credentials.

Owns the keypair lifecycle, drives the registration handshake, decrypts
inbound payloads, and pokes the XMPP client awake on wake events. Intended
to be constructed once per app and held by the Application.
"""

from __future__ import annotations

import json
import logging

from gi.repository import Gio, GLib, GObject

from patch import APP_ID
from patch import account as account_mod
from patch.push import decrypt as decrypt_mod
from patch.push import keys as keys_mod
from patch.push.connector   import PushConnector
from patch.push.distributor import PushDistributor
from patch.push.enable_iq   import build_enable_iq

log = logging.getLogger(__name__)


class PushController(GObject.Object):
    __gtype_name__ = "PatchPushController"

    def __init__(self, account, xmpp):
        super().__init__()
        self._account = account
        self._xmpp = xmpp
        self._settings = Gio.Settings.new(APP_ID)

        self._connector = PushConnector()
        self._distributor = PushDistributor()

        # We re-issue Register on every startup per the UP spec
        # recommendation. Connector1.NewEndpoint drives the XEP-0357
        # enable IQ once we know what endpoint to advertise.
        self._connector.connect("new-endpoint", self._on_new_endpoint)
        self._connector.connect("message",      self._on_message)
        self._connector.connect("unregistered", self._on_unregistered)
        self._connector.connect("registration-failed", self._on_registration_failed)

        # When the XMPP stream first comes up after we have a stored
        # endpoint, send the enable IQ. Subsequent reconnects don't
        # re-enable — the server keeps the registration across our
        # disconnects (XEP-0357 is stateful at the server end).
        self._xmpp.connect("state-changed", self._on_xmpp_state)

    def start(self) -> None:
        """Publish the Connector1 service and kick off Register."""
        self._connector.publish()
        # Defer the Register call so we don't block do_activate behind
        # session-bus stalls.
        GLib.idle_add(self._distributor.register)

    # -- inbound from distributor -----------------------------------------

    def _on_new_endpoint(self, _connector, endpoint: str):
        log.info("registered UP endpoint: %s", endpoint)
        # Persist for debug UI + so reconnects don't trigger a re-register
        # storm. If the endpoint changed (distributor rotation), the
        # server keeps the previous one too — mod_cloud_notify keys
        # registrations by node, and we always use the same node.
        self._settings.set_string("push-endpoint", endpoint)
        self._maybe_send_enable_iq()

    def _on_message(self, _connector, body: bytes, message_id: str):
        log.info("push delivered, id=%s len=%d", message_id, len(body))
        plaintext = self._decrypt(body)
        if plaintext is None:
            return
        try:
            payload = json.loads(plaintext)
        except json.JSONDecodeError:
            log.warning("push payload not JSON; treating as raw wake signal")
            payload = {}
        log.info("push payload: %s", payload)
        # Wake the XMPP stream — for the warm-resident path the client is
        # already connected and this is a no-op; for cold-start we just
        # came up and need to actually log in.
        self._xmpp.connect_to_server()

    def _on_unregistered(self, _connector):
        # The distributor revoked us. Clear the endpoint so we know to
        # re-register on the next start.
        log.info("unregistered by distributor — clearing endpoint")
        self._settings.set_string("push-endpoint", "")

    def _on_registration_failed(self, _connector, reason):
        log.warning("UP registration failed: %s", reason)
        self._settings.set_string("push-endpoint", "")

    # -- outbound to server -----------------------------------------------

    def _on_xmpp_state(self, _xmpp, state):
        if state == account_mod.STATE_CONNECTED:
            self._maybe_send_enable_iq()

    def _maybe_send_enable_iq(self) -> None:
        endpoint = self._settings.get_string("push-endpoint")
        if not endpoint:
            return
        if self._account.state != account_mod.STATE_CONNECTED:
            return
        if not self._account.is_configured:
            return
        keys = keys_mod.load_or_generate(self._account.jid)
        iq = build_enable_iq(
            to_jid=self._account.jid,
            push_jid=self._account.jid,
            node="up-default",
            endpoint=endpoint,
            p256dh_b64url=keys.public_b64url(),
            auth_b64url=keys.auth_b64url(),
        )
        try:
            self._xmpp._client.send_stanza(iq)
        except Exception as exc:
            log.warning("enable IQ send failed: %s", exc)
            return
        log.info("sent XEP-0357 enable IQ for endpoint %s", endpoint)

    # -- decryption -------------------------------------------------------

    def _decrypt(self, body: bytes) -> bytes | None:
        if not self._account.is_configured:
            return None
        keys = keys_mod.load(self._account.jid)
        if keys is None:
            log.warning("no UP keypair stored — can't decrypt")
            return None
        try:
            return decrypt_mod.decrypt(body, keys.private_scalar, keys.auth_secret)
        except Exception as exc:
            log.warning("push decrypt failed: %s", exc)
            return None
