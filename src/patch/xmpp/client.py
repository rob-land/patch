"""Thin wrapper over `nbxmpp.client.Client` that:

- maps the nbxmpp Observable signal API to the GTK side as a GObject
- drives the connection lifecycle from the Account model's credentials
- emits two outward signals: `message-received` and `state-changed`

We use nbxmpp (Gajim's library) instead of slixmpp because nbxmpp drives
its own I/O off the default GLib mainloop — no asyncio worker thread,
no marshalling. The trade-off is a smaller community than slixmpp; for
the JMP-specific stanza shapes we care about that's fine.

Phase 1 scope: connect, authenticate, receive chat messages, send chat
messages. Stream Management, MAM catch-up, presence broadcasting, OMEMO,
Jingle Message Initiation all land in later phases.
"""

from __future__ import annotations

import logging
from typing import Optional

from gi.repository import GObject

from nbxmpp.client import Client as NbxClient
from nbxmpp.const import ConnectionType, StreamError
from nbxmpp.protocol import JID, Message
from nbxmpp.structs import MessageProperties

from patch import account as account_mod

log = logging.getLogger(__name__)


class XmppClient(GObject.Object):
    __gtype_name__ = "PatchXmppClient"

    __gsignals__ = {
        # remote_jid (bare, str), body (str), incoming (bool), timestamp (float)
        "message-received": (GObject.SignalFlags.RUN_FIRST, None, (str, str, bool, float)),
        # state string — matches account.STATE_*
        "state-changed":    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, account):
        super().__init__()
        self._account = account
        self._client: Optional[NbxClient] = None
        # Reconnect counter, reset on successful connect. Used to back off
        # in `request_reconnect` so we don't hammer the server when wifi
        # is down.
        self._fail_count = 0

    # -- lifecycle --------------------------------------------------------

    def connect_to_server(self) -> None:
        if not self._account.is_configured:
            log.debug("connect: no account configured, skipping")
            return
        if self._client is not None:
            log.debug("connect: already have a client; disconnect first")
            return

        password = self._account.get_password()
        if not password:
            log.warning("connect: no password in keyring for %s", self._account.jid)
            self._account.set_state(account_mod.STATE_FAILED,
                                    "Password missing from keyring")
            return

        try:
            jid = JID.from_string(self._account.jid)
        except Exception as exc:
            log.warning("connect: bad JID %r: %s", self._account.jid, exc)
            self._account.set_state(account_mod.STATE_FAILED, f"Invalid JID: {exc}")
            return

        client = NbxClient(log_context="patch")
        client.set_domain(jid.domain)
        client.set_username(jid.localpart)
        client.set_resource("patch")
        client.set_password(password)
        client.set_ignore_tls_errors(False)
        # Prefer direct TLS (5223) — chat.rob.land advertises both _xmpps
        # and _xmpp SRV. nbxmpp will try them in order.
        client.set_connection_types([ConnectionType.DIRECT_TLS,
                                     ConnectionType.START_TLS])

        # Wire the nbxmpp Observable signals to our model.
        client.subscribe("connected",         self._on_connected)
        client.subscribe("disconnected",      self._on_disconnected)
        client.subscribe("connection-failed", self._on_connection_failed)
        client.subscribe("login-successful",  self._on_login_successful)
        client.subscribe("stanza-received",   self._on_stanza_received)

        self._client = client
        self._account.set_state(account_mod.STATE_CONNECTING)
        log.info("connecting %s -> %s", self._account.jid, jid.domain)
        client.connect()

    def disconnect_from_server(self) -> None:
        if self._client is None:
            return
        log.info("disconnecting")
        try:
            self._client.disconnect()
        finally:
            self._client = None
            self._account.set_state(account_mod.STATE_DISCONNECTED)
            self.emit("state-changed", account_mod.STATE_DISCONNECTED)

    # -- send -------------------------------------------------------------

    def send_chat_message(self, to_jid: str, body: str) -> bool:
        if not self._client or not self._client.is_stream_authenticated:
            log.warning("send: not connected")
            return False
        msg = Message(to=to_jid, typ="chat", body=body)
        try:
            self._client.send_stanza(msg)
        except Exception as exc:
            log.exception("send failed: %s", exc)
            return False
        # Local echo so the conversation list updates immediately. The
        # MAM/carbons round-trip will happen later in flight.
        from time import time as now
        self.emit("message-received", to_jid, body, False, now())
        return True

    # -- nbxmpp signal handlers ------------------------------------------

    def _on_connected(self, _client, _signal_name):
        log.info("transport connected")

    def _on_login_successful(self, _client, _signal_name):
        log.info("login successful")
        self._fail_count = 0
        self._account.set_state(account_mod.STATE_CONNECTED)
        self.emit("state-changed", account_mod.STATE_CONNECTED)

    def _on_disconnected(self, _client, _signal_name):
        log.info("disconnected")
        self._account.set_state(account_mod.STATE_DISCONNECTED)
        self.emit("state-changed", account_mod.STATE_DISCONNECTED)
        # In Phase 1 we don't auto-reconnect. Phase 2 (UnifiedPush wake)
        # is the proper trigger for a reconnect; otherwise the user can
        # toggle from the menu.

    def _on_connection_failed(self, _client, _signal_name):
        self._fail_count += 1
        err = self._client.get_error() if self._client else None
        msg = str(err) if err else "connection failed"
        log.warning("connection failed (#%d): %s", self._fail_count, msg)
        self._account.set_state(account_mod.STATE_FAILED, msg)
        self.emit("state-changed", account_mod.STATE_FAILED)
        self._client = None

    def _on_stanza_received(self, _client, _signal_name, stanza):
        # We only handle <message> here. Presence, IQ, etc. flow into
        # nbxmpp's internal dispatcher modules and we can hook those
        # separately if needed.
        if stanza.getName() != "message":
            return
        self._handle_message(stanza)

    # -- message ingest ---------------------------------------------------

    def _handle_message(self, stanza: Message) -> None:
        body = stanza.getBody()
        if not body:
            # Receipt, chat state, marker, etc. — nothing to surface.
            return
        from_jid = stanza.getFrom()
        if from_jid is None:
            return
        bare = from_jid.bare
        # Convert the XMPP delay timestamp if present (MAM, offline store).
        from time import time as now
        timestamp = now()
        log.info("message from %s: %s", bare, body[:80])
        self.emit("message-received", str(bare), body, True, timestamp)
