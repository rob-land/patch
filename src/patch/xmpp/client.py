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

from gi.repository import GLib, GObject

from nbxmpp.client import Client as NbxClient
from nbxmpp.const import ConnectionType
from nbxmpp.modules.misc import unwrap_mam
from nbxmpp.namespaces import Namespace
from nbxmpp.protocol import JID, Iq, Message

from patch import account as account_mod

log = logging.getLogger(__name__)


JMI_NS = "urn:xmpp:jingle-message:0"
OOB_NS = "jabber:x:oob"

# Exponential backoff schedule for reconnection. Capped at 5 minutes — past
# that point we trust the manual "Connect" action or a UnifiedPush wake to
# bring us back instead of churning the radio.
_BACKOFF_SECONDS = [2, 5, 10, 30, 60, 120, 300]


def _backoff_delay(fail_count: int) -> int:
    return _BACKOFF_SECONDS[min(fail_count - 1, len(_BACKOFF_SECONDS) - 1)]


class XmppClient(GObject.Object):
    __gtype_name__ = "PatchXmppClient"

    __gsignals__ = {
        # remote_jid (bare, str), body (str), incoming (bool), timestamp (float),
        # attachment_url (str, "" if none)
        "message-received": (GObject.SignalFlags.RUN_FIRST, None,
                             (str, str, bool, float, str)),
        # state string — matches account.STATE_*
        "state-changed":    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # XEP-0353 Jingle Message Initiation:
        # action ("propose" | "proceed" | "accept" | "reject" | "retract")
        # session_id (str)
        # peer_jid (str, full)        — the JID we're talking to
        # incoming (bool)             — was this stanza FROM the peer?
        "jmi-event":        (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, bool)),
    }

    def __init__(self, account, store=None):
        super().__init__()
        self._account = account
        self._store = store        # optional; only used for MAM catch-up
        self._client: Optional[NbxClient] = None
        # Reconnect counter, reset on successful connect. Drives the
        # exponential backoff schedule in `_schedule_reconnect`.
        self._fail_count = 0
        # GLib timeout source ID for the scheduled reconnect, so we can
        # cancel it on manual disconnect or a successful connect.
        self._reconnect_source: int = 0
        # Whether the user (or push controller) wants us connected. When
        # False, disconnect requests are honoured and reconnect is not
        # attempted. Flipped to True the first time connect_to_server is
        # called and back to False on disconnect_from_server.
        self._want_connected = False

    # -- lifecycle --------------------------------------------------------

    def connect_to_server(self) -> None:
        # Flip the "want connected" flag on every entry so a push wake
        # that fires before the user dismisses an offline state still
        # arms reconnect.
        self._want_connected = True
        # If a backoff timer is pending, drop it and try immediately.
        self._cancel_reconnect()
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

        # Unique resource per process: cold-start activations would
        # otherwise conflict-replace the previous still-hibernated
        # `patch` resource and evict its queued messages before we have
        # a chance to receive them. A random suffix keeps each launch
        # cleanly independent; carbons + MAM cover the cross-resource
        # delivery we still want.
        import os
        resource = f"patch.{os.urandom(4).hex()}"
        client = NbxClient(log_context="patch")
        client.set_domain(jid.domain)
        client.set_username(jid.localpart)
        client.set_resource(resource)
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
        self._want_connected = False
        self._cancel_reconnect()
        if self._client is None:
            self._account.set_state(account_mod.STATE_DISCONNECTED)
            self.emit("state-changed", account_mod.STATE_DISCONNECTED)
            return
        log.info("disconnecting")
        try:
            self._client.disconnect()
        finally:
            self._client = None
            self._account.set_state(account_mod.STATE_DISCONNECTED)
            self.emit("state-changed", account_mod.STATE_DISCONNECTED)

    def _cancel_reconnect(self) -> None:
        if self._reconnect_source:
            GLib.source_remove(self._reconnect_source)
            self._reconnect_source = 0

    def _schedule_reconnect(self) -> None:
        if not self._want_connected:
            return
        if self._reconnect_source:
            return
        delay = _backoff_delay(self._fail_count)
        log.info("scheduling reconnect in %ds (fail #%d)", delay, self._fail_count)
        def _fire():
            self._reconnect_source = 0
            self.connect_to_server()
            return False
        self._reconnect_source = GLib.timeout_add_seconds(delay, _fire)

    # -- send -------------------------------------------------------------

    def send_chat_message(self, to_jid: str, body: str,
                          attachment_url: str = "") -> bool:
        if not self._client or not self._client.is_stream_authenticated:
            log.warning("send: not connected")
            return False
        msg = Message(to=to_jid, typ="chat", body=body)
        if attachment_url:
            # XEP-0066 Out-of-Band Data — JMP/cheogram looks for the
            # url here to ship MMS images out to PSTN.
            oob = msg.addChild("x", namespace=OOB_NS)
            oob.addChild("url").addData(attachment_url)
        try:
            self._client.send_stanza(msg)
        except Exception as exc:
            log.exception("send failed: %s", exc)
            return False
        # Local echo so the conversation list updates immediately. The
        # MAM/carbons round-trip will happen later in flight.
        from time import time as now
        self.emit("message-received", to_jid, body, False, now(),
                  attachment_url)
        return True

    # -- XEP-0363 HTTP Upload --------------------------------------------

    def request_upload_slot(self, upload_jid: str, filename: str,
                             size: int, content_type: str, callback) -> None:
        """Async wrapper around nbxmpp's HTTPUpload.request_slot.

        callback signature: (put_url, get_url, put_headers, error).
        On success error is None; on failure put/get are "" and error is a
        string. The headers dict carries auth/cookie headers the slot
        requires for the PUT request.
        """
        if not self._client or not self._client.is_stream_authenticated:
            callback("", "", {}, "not connected")
            return
        try:
            mod = self._client.get_module("HTTPUpload")
        except Exception as exc:  # noqa: BLE001
            callback("", "", {}, "no HTTPUpload module: " + str(exc))
            return

        def _on_done(task):
            try:
                slot = task.finish()
            except Exception as exc:  # noqa: BLE001
                callback("", "", {}, str(exc))
                return
            put_url = getattr(slot, "put_uri", None) or getattr(slot, "put", "")
            get_url = getattr(slot, "get_uri", None) or getattr(slot, "get", "")
            headers = getattr(slot, "headers", {}) or {}
            callback(put_url, get_url, headers, None)

        try:
            mod.request_slot(
                jid=JID.from_string(upload_jid),
                filename=filename, size=size,
                content_type=content_type,
                callback=_on_done,
            )
        except Exception as exc:  # noqa: BLE001
            callback("", "", {}, str(exc))

    # -- nbxmpp signal handlers ------------------------------------------

    def _on_connected(self, _client, _signal_name):
        # nbxmpp fires this once the stream goes ACTIVE — auth done, bind
        # done, ready to send and receive. (`login-successful` only fires
        # in nbxmpp's `login-test` mode where the client disconnects
        # immediately after auth, so we can't hook our session-up
        # behaviour there.)
        log.info("logged in (stream active)")
        self._fail_count = 0
        self._cancel_reconnect()
        # Send a bare presence so the server knows we're available and
        # starts delivering carbons / push for us. Without this some
        # servers hold messages in offline-store rather than forwarding
        # to the live stream.
        try:
            from nbxmpp.protocol import Presence
            self._client.send_stanza(Presence())
        except Exception as exc:  # noqa: BLE001
            log.debug("initial presence failed: %s", exc)
        # Enable XEP-0280 Message Carbons so messages we send (or that
        # are sent on our behalf) from other clients on this account
        # show up here too. nbxmpp doesn't ship a Carbons module — we
        # ship the enable IQ inline.
        try:
            enable_iq = Iq(typ="set")
            enable_iq.addChild("enable", namespace=Namespace.CARBONS)
            self._client.send_stanza(enable_iq)
        except Exception as exc:  # noqa: BLE001
            log.debug("carbons enable failed: %s", exc)
        self._account.set_state(account_mod.STATE_CONNECTED)
        self.emit("state-changed", account_mod.STATE_CONNECTED)
        # MAM catch-up off by default for now — nbxmpp 7.2 chokes on
        # large concatenated batches with an ExpatError that tears the
        # whole stream down. Live stanzas come through stanza-received
        # without MAM. Toggle PATCH_MAM_CATCHUP=1 in the env to opt in.
        import os
        if os.environ.get("PATCH_MAM_CATCHUP") == "1":
            self._request_mam_catchup()

    def _on_login_successful(self, _client, _signal_name):
        # Only fires in login-test mode (see _on_connected). Kept as a
        # no-op so the subscribe call doesn't throw if the upstream
        # behaviour ever changes.
        log.debug("login-successful (login-test mode only)")

    def _on_disconnected(self, _client, _signal_name):
        log.info("disconnected")
        self._client = None
        self._account.set_state(account_mod.STATE_DISCONNECTED)
        self.emit("state-changed", account_mod.STATE_DISCONNECTED)
        # Lost a connection we wanted to keep — back off and retry.
        if self._want_connected:
            self._fail_count += 1
            self._schedule_reconnect()

    def _on_connection_failed(self, _client, _signal_name):
        self._fail_count += 1
        err = self._client.get_error() if self._client else None
        msg = str(err) if err else "connection failed"
        log.warning("connection failed (#%d): %s", self._fail_count, msg)
        self._client = None
        self._account.set_state(account_mod.STATE_FAILED, msg)
        self.emit("state-changed", account_mod.STATE_FAILED)
        self._schedule_reconnect()

    def _on_stanza_received(self, _client, _signal_name, stanza):
        # We only handle <message> here. Presence, IQ, etc. flow into
        # nbxmpp's internal dispatcher modules and we can hook those
        # separately if needed.
        if stanza.getName() != "message":
            return

        own_jid = JID.from_string(self._account.jid)

        # XEP-0280 Message Carbons wrap a message sent or received
        # through another client on our account:
        #   <message from="us@server"><sent|received xmlns="urn:xmpp:carbons:2">
        #     <forwarded xmlns="urn:xmpp:forward:0">
        #       <message>...the original...</message>
        #     </forwarded>
        #   </sent|received></message>
        # We do the unwrap ourselves rather than calling
        # nbxmpp.modules.misc.unwrap_carbon because that helper uses
        # Message.getFrom() which a raw simplexml Node doesn't have.
        inner = stanza
        for tag_name in ("received", "sent"):
            carbon = stanza.getTag(tag_name, namespace=Namespace.CARBONS)
            if carbon is None:
                continue
            # The outer "from" must be our own bare JID; servers shouldn't
            # accept anything else but verify defensively.
            outer_from = stanza.getAttr("from") or ""
            outer_bare = outer_from.split("/", 1)[0]
            if outer_bare != str(own_jid.bare):
                log.debug("rejecting carbon from %s (not us)", outer_from)
                return
            forwarded = carbon.getTag("forwarded", namespace=Namespace.FORWARD)
            if forwarded is None:
                break
            inner_msg = forwarded.getTag("message")
            if inner_msg is None:
                break
            inner = inner_msg
            # `received` carbons of our OWN sent messages are a duplicate
            # of the `sent` carbon we already handled — drop them.
            if (tag_name == "received"
                and (inner.getAttr("from") or "").split("/", 1)[0] == str(own_jid.bare)):
                return
            break

        # MAM catch-up results arrive wrapped:
        #   <message><result xmlns="urn:xmpp:mam:2"><forwarded>
        #     <message>...the original...</message>
        #   </forwarded></result></message>
        try:
            inner, mam_data = unwrap_mam(inner, own_jid)
        except Exception as exc:  # noqa: BLE001
            log.debug("MAM unwrap failed (treating as direct): %s", exc)
            mam_data = None

        # XEP-0353 Jingle Message Initiation. The stanza is a <message>
        # with no body — just a propose/proceed/accept/reject/retract
        # child tagged with urn:xmpp:jingle-message:0. Surface as a
        # typed signal so the call UI can drive state without grepping
        # raw stanzas.
        if self._handle_jmi(inner):
            return

        if mam_data is not None:
            # MAM result: use the archived timestamp, not "now".
            self._handle_message(inner, timestamp=mam_data.timestamp,
                                 from_mam=True)
        else:
            self._handle_message(inner)

    # -- message ingest ---------------------------------------------------

    def _handle_message(self, stanza, timestamp: float | None = None,
                        from_mam: bool = False) -> None:
        # Use Node-safe accessors. The dispatcher hands us raw simplexml
        # Nodes (not typed Message instances) for stanza-received, so
        # `stanza.getBody()` AttributeErrors on otherwise valid messages.
        body = stanza.getTagData("body")
        # XEP-0066 Out-of-Band Data — JMP/cheogram attaches MMS image
        # URLs as <x xmlns="jabber:x:oob"><url>...</url></x>. The body
        # typically mirrors the URL but may carry caption text.
        attachment_url = ""
        oob = stanza.getTag("x", namespace=OOB_NS)
        if oob is not None:
            attachment_url = oob.getTagData("url") or ""
        if not body and not attachment_url:
            # Receipt, chat state, marker, etc. — nothing to surface.
            return
        # If we have an attachment but no body, default the body to the
        # URL so the conversation list preview has something to show.
        if not body:
            body = attachment_url
        from_str = stanza.getAttr("from")
        if not from_str:
            return
        try:
            from_jid = JID.from_string(from_str)
        except Exception:  # noqa: BLE001
            return
        bare = str(from_jid.bare)
        if timestamp is None:
            from time import time as now
            timestamp = now()
        # When the message came from us in MAM (sent via another client
        # or our own outbound echo), the "from" is our own JID. Surface
        # those as outbound so the conversation list renders correctly.
        own_bare = str(JID.from_string(self._account.jid).bare)
        incoming = bare != own_bare
        # For an outbound MAM result, the conversation key is the "to" JID
        # of the original message, not the "from".
        if not incoming:
            to_str = stanza.getAttr("to")
            if to_str:
                try:
                    bare = str(JID.from_string(to_str).bare)
                except Exception:  # noqa: BLE001
                    pass
        log.info("%smessage %s %s: %s%s",
                 "[mam] " if from_mam else "",
                 "<-" if incoming else "->",
                 bare, body[:80],
                 (" [oob " + attachment_url + "]") if attachment_url else "")
        self.emit("message-received", bare, body, incoming, timestamp,
                  attachment_url)

    # -- XEP-0353 Jingle Message Initiation ------------------------------

    _JMI_ACTIONS = ("propose", "proceed", "accept", "reject", "retract")

    def _handle_jmi(self, stanza) -> bool:
        """Detect and surface a JMI message. Returns True if handled."""
        for action in self._JMI_ACTIONS:
            tag = stanza.getTag(action, namespace=JMI_NS)
            if tag is None:
                continue
            session_id = tag.getAttr("id") or ""
            from_str = stanza.getAttr("from") or ""
            try:
                peer = str(JID.from_string(from_str))
            except Exception:  # noqa: BLE001
                peer = from_str
            own_bare = str(JID.from_string(self._account.jid).bare)
            incoming = (from_str.split("/", 1)[0] != own_bare)
            log.info("JMI %s %s id=%s peer=%s",
                     action, "<-" if incoming else "->", session_id, peer)
            self.emit("jmi-event", action, session_id, peer, incoming)
            return True
        return False

    def send_jmi(self, action: str, session_id: str, peer_jid: str,
                 media: str = "audio") -> bool:
        """Send a JMI stanza. `media` only matters for propose."""
        if action not in self._JMI_ACTIONS:
            raise ValueError("unknown JMI action: " + action)
        if not self._client or not self._client.is_stream_authenticated:
            log.warning("send_jmi: not connected");
            return False
        msg = Message(to=peer_jid)
        elem = msg.addChild(action, namespace=JMI_NS, attrs={"id": session_id})
        if action == "propose":
            # XEP-0353 requires at least one <description> child describing
            # the media we'd negotiate. Audio with the RTP namespace covers
            # PSTN calls through cheogram/JMP.
            elem.addChild(
                "description",
                namespace="urn:xmpp:jingle:apps:rtp:1",
                attrs={"media": media},
            )
        try:
            self._client.send_stanza(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_jmi failed: %s", exc)
            return False
        log.info("JMI %s -> id=%s peer=%s", action, session_id, peer_jid)
        return True

    # -- MAM catch-up ----------------------------------------------------

    def _request_mam_catchup(self) -> None:
        if self._client is None or self._store is None:
            return
        try:
            mam = self._client.get_module("MAM")
        except Exception as exc:  # noqa: BLE001
            log.warning("MAM module unavailable: %s", exc)
            return

        import datetime as dt
        latest = self._store.latest_timestamp()
        if latest > 0:
            start = dt.datetime.fromtimestamp(latest, tz=dt.timezone.utc)
        else:
            # First-ever connect: limit to the last day so we don't drag
            # in months of unrelated archive.
            start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        own_jid = JID.from_string(self._account.jid)
        queryid = "patch-catchup"
        log.info("MAM catch-up from %s", start.isoformat(timespec="seconds"))
        # nbxmpp 7.2 has a known issue parsing large concatenated MAM
        # batches in one TCP read; the SimpleXML parser misinterprets
        # the byte stream as "stream finished" mid-blob and tears the
        # connection down. Keep `max_` small so each batch fits in one
        # TCP segment. TODO: paginate via RSM cursor for full history.
        try:
            mam.make_query(
                jid=own_jid.bare,
                queryid=queryid,
                start=start,
                max_=20,
                callback=self._on_mam_query_done,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("MAM query failed to dispatch: %s", exc)

    def _on_mam_query_done(self, task):
        try:
            result = task.finish()
        except Exception as exc:  # noqa: BLE001
            log.warning("MAM catch-up failed: %s", exc)
            return
        log.info("MAM catch-up done: complete=%s", getattr(result, "complete", "?"))
