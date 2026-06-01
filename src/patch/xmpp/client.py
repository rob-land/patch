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

from gi.repository import Gio, GLib, GObject

from nbxmpp.client import Client as NbxClient
from nbxmpp.const import ConnectionType
from nbxmpp.modules.misc import unwrap_mam
from nbxmpp.namespaces import Namespace
from nbxmpp.protocol import JID, Iq, Message

from patch.xmpp import jingle as jingle_mod
from patch.xmpp.turn import fetch_turn_uris

from patch import APP_ID
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


def _build_quote_prefix(target_body: str) -> str:
    """Render a quoted prefix for a XEP-0461 reply body.

    Each line of ``target_body`` gets a leading "> ", then a blank
    line separates the quote from the reply text. Returns "" when the
    target body is empty — the reply still carries the <reply/>
    element, just without a fallback range."""
    text = (target_body or "").strip()
    if not text:
        return ""
    lines = [f"> {ln}" if ln else ">" for ln in text.splitlines()]
    return "\n".join(lines) + "\n\n"


class XmppClient(GObject.Object):
    __gtype_name__ = "PatchXmppClient"

    __gsignals__ = {
        # remote_jid (bare, str), body (str), incoming (bool), timestamp (float),
        # attachment_url (str, "" if none), message_id (str, the stanza id —
        # used to correlate XEP-0184 delivery receipts), reply_to_id (str,
        # the stanza id of the message this one is a XEP-0461 reply to;
        # empty when not a reply).
        "message-received": (GObject.SignalFlags.RUN_FIRST, None,
                             (str, str, bool, float, str, str, str)),
        # state string — matches account.STATE_*
        "state-changed":    (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # message_id (str), new delivery state (str: 'delivered' for now).
        # Fires when an inbound XEP-0184 <received/> ties back to one of
        # our outgoing messages.
        "message-receipt":  (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # XEP-0085: remote_jid (bare, str), state (str: 'active' /
        # 'composing' / 'paused' / 'inactive' / 'gone'). Cheogram
        # strips these on the SMS leg; mostly useful for direct XMPP.
        "chat-state":       (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # XEP-0444: target_msg_id (str — id of the message reacted to),
        # sender_jid (bare, str), remote_jid (the conversation, str),
        # emojis (object, a list[str]). One signal per reactions
        # stanza; the emoji list REPLACES sender_jid's prior set.
        "reaction-received": (GObject.SignalFlags.RUN_FIRST, None,
                              (str, str, str, object)),
        # XEP-0308: target_msg_id (str — id of the message being
        # corrected), conv_jid (str), new_body (str), timestamp (float).
        # Fires when the original sender publishes an edit. Persister
        # rewrites the row's body and stamps corrected_at.
        "message-corrected": (GObject.SignalFlags.RUN_FIRST, None,
                              (str, str, str, float)),
        # XEP-0353 Jingle Message Initiation:
        # action ("propose" | "proceed" | "accept" | "reject" | "retract")
        # session_id (str)
        # peer_jid (str, full)        — the JID we're talking to
        # incoming (bool)             — was this stanza FROM the peer?
        "jmi-event":        (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, bool)),
        # Parsed Jingle iq (dict per xmpp.jingle.parse_jingle), full FROM
        # of the inbound iq, and the iq id for the caller to ACK.
        "jingle-iq":        (GObject.SignalFlags.RUN_FIRST, None, (object, str, str)),
    }

    def __init__(self, account, store=None):
        super().__init__()
        self._settings = Gio.Settings.new(APP_ID)
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
        self._mam_syncing = False
        # XEP-0215 TURN URI cache. Pre-fetched after login so the per-call
        # path doesn't pay the disco round-trip. coturn defaults to 1h
        # credential lifetime; we re-fetch at 30 min to stay comfortably
        # inside that. Stores all advertised URIs (UDP/TCP/TURNS) so
        # ICE can probe multiple transports; cleared on disconnect.
        self._turn_uris: list[str] = []
        self._turn_uri_fetched_at: float = 0.0
        self._turn_generation: int = 0

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
        # Keep an existing client across involuntary disconnects so
        # XEP-0198 smacks can resume the session — nbxmpp's connect()
        # takes the "Reconnect" branch when called on a prior-successful
        # client, replaying the resolved addresses and sending <resume/>
        # on the new stream. If smacks resume succeeds, the server
        # replays any stanzas it queued while we were offline; if it
        # fails (timed out etc.) nbxmpp falls through to a fresh bind.
        if self._client is not None and self._client.is_stream_authenticated:
            log.debug("connect: stream already authenticated")
            return
        if self._client is not None:
            self._account.set_state(account_mod.STATE_CONNECTING)
            log.info("reconnecting (smacks resume if supported)")
            self._client.connect()
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

        # Per-install resource: stable across runs on the same device,
        # unique across installs so two devices (e.g. phone + tablet)
        # don't conflict-kick each other. Same-device reconnects still
        # replace the prior session (preventing zombie accumulation).
        # The suffix is generated once and persisted in gsettings.
        import secrets
        settings = Gio.Settings.new(APP_ID)
        suffix = settings.get_string("resource-id")
        if not suffix:
            suffix = secrets.token_hex(4)
            settings.set_string("resource-id", suffix)
        resource = f"patch.{suffix}"
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

        # Register a real dispatcher handler for jingle iqs — listening
        # to 'stanza-received' alone is NOT enough, because nbxmpp's
        # dispatcher follows up the unhandled-iq chain with
        # _default_handler which sends <feature-not-implemented/>. The
        # peer sees that and gives up. By raising NodeProcessed inside
        # this handler we suppress the default reply.
        from nbxmpp.structs import StanzaHandler
        from patch.xmpp.jingle import NS_JINGLE
        client.register_handler(StanzaHandler(
            name="iq", callback=self._on_jingle_iq_handler,
            typ="set", ns=NS_JINGLE))

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
        log.info("disconnecting (graceful — closes smacks session)")
        try:
            # immediate=False lets nbxmpp send <r/>, ack, and close the
            # SM session cleanly so the server doesn't keep it pending.
            self._client.disconnect(immediate=False)
        finally:
            # Clear here, not in _on_disconnected — _on_disconnected
            # keeps the client so involuntary disconnects can resume.
            self._client = None
            self._turn_uris = []
            self._turn_uri_fetched_at = 0.0
            self._account.set_state(account_mod.STATE_DISCONNECTED)
            self.emit("state-changed", account_mod.STATE_DISCONNECTED)

    def _cancel_reconnect(self) -> None:
        if self._reconnect_source:
            GLib.source_remove(self._reconnect_source)
            self._reconnect_source = 0

    # -- TURN URI cache ---------------------------------------------------

    # 30 minutes — half the typical coturn 1h credential lifetime, so we
    # never hand out a URI that's about to expire mid-call.
    _TURN_CACHE_TTL = 30 * 60

    def get_turn_uris(self, callback) -> None:
        """Deliver fresh-enough TURN URIs to ``callback(list[str])``.

        Cached across calls: re-fetched only when the cache is empty or
        the prior fetch is older than ``_TURN_CACHE_TTL``. Always async —
        the callback is invoked via the main loop even on a cache hit,
        so callers can rely on the same control-flow shape regardless.
        Order is UDP → TCP → TURNS so ICE prefers UDP relay candidates
        but can fall through to TCP if UDP/3478 is firewalled.
        """
        import time
        now = time.monotonic()
        if self._turn_uris and (now - self._turn_uri_fetched_at) < self._TURN_CACHE_TTL:
            GLib.idle_add(lambda: (callback(list(self._turn_uris)), False)[1])
            return
        server = self._account.jid.split("@", 1)[-1]
        if not server or self._client is None:
            GLib.idle_add(lambda: (callback([]), False)[1])
            return
        self._turn_generation += 1
        gen = self._turn_generation
        def _on_fetched(uris):
            if gen != self._turn_generation:
                return
            if uris:
                self._turn_uris = uris
                self._turn_uri_fetched_at = time.monotonic()
                log.info("TURN URIs cached (live disco hit): %d", len(uris))
            callback(uris)
        fetch_turn_uris(self, server, _on_fetched)

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
                          attachment_url: str = "",
                          reply_to: dict | None = None,
                          replace_id: str = "") -> bool:
        """Send a chat message.

        ``reply_to`` (XEP-0461 quoted reply) is an optional dict with
        ``target_id`` (the stanza id being replied to) and
        ``target_body`` (the body of that message, used to build the
        plain-text quoted prefix that non-aware clients render).

        ``replace_id`` (XEP-0308 correction) is the stanza id of the
        original message this one supersedes. Aware clients (and
        cheogram, within its ~5 min SMS-edit window) replace the
        original in place; legacy clients see two separate messages.
        """
        if not self._client or not self._client.is_stream_authenticated:
            log.warning("send: not connected")
            return False
        import uuid
        stanza_id = "patch-" + uuid.uuid4().hex
        # If this is a reply, prepend a quoted prefix to the body and
        # advertise the byte range via XEP-0428 fallback so aware
        # clients strip it before rendering. The wire body keeps the
        # full text so SMS/legacy clients still see context.
        final_body = body
        fallback_end = 0
        if reply_to and reply_to.get("target_id"):
            quoted = _build_quote_prefix(reply_to.get("target_body") or "")
            if quoted:
                final_body = quoted + body
                fallback_end = len(quoted.encode("utf-8"))
        msg = Message(to=to_jid, typ="chat", body=final_body)
        msg.setAttr("id", stanza_id)
        if replace_id:
            msg.addChild("replace", namespace=Namespace.CORRECT,
                         attrs={"id": replace_id})
        if reply_to and reply_to.get("target_id"):
            reply_el = msg.addChild("reply", namespace=Namespace.REPLY,
                                    attrs={"id": reply_to["target_id"]})
            target_jid = reply_to.get("target_jid")
            if target_jid:
                reply_el.setAttr("to", target_jid)
            if fallback_end:
                fb = msg.addChild("fallback", namespace=Namespace.FALLBACK,
                                  attrs={"for": Namespace.REPLY})
                fb.addChild("body", attrs={"start": "0",
                                            "end": str(fallback_end)})
        # XEP-0184 receipt request — cheogram surfaces SMS delivery
        # status reports back via <received id='..'/>. Direct XMPP
        # peers also honour this and we render a 'delivered' tick.
        msg.addChild("request", namespace=Namespace.RECEIPTS)
        # XEP-0334 hint: ask the server (and any aware client) to MAM
        # this even if it would otherwise be filtered.
        msg.addChild("store", namespace=Namespace.HINTS)
        # XEP-0085 chat state. Bundled in the message body itself
        # ("active" = focused on the conversation). The standalone
        # composing/paused/inactive states would need typing-detection
        # plumbing on the compose entry — not wired up yet.
        msg.addChild("active", namespace=Namespace.CHATSTATES)
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
        if replace_id:
            # A correction REPLACES an existing row; don't append a
            # new one. Use the conv jid (bare to_jid) for routing.
            try:
                conv_jid = str(JID.from_string(to_jid).bare)
            except Exception:  # noqa: BLE001
                conv_jid = to_jid
            self.emit("message-corrected", replace_id, conv_jid, body, now())
            return True
        # Show only the reply text in the echoed body (the fallback
        # prefix is a wire-format detail for non-aware clients).
        echo_body = body
        echo_reply_id = (reply_to.get("target_id") if reply_to else "") or ""
        self.emit("message-received", to_jid, echo_body, False, now(),
                  attachment_url, stanza_id, echo_reply_id)
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
        # Send a presence with high priority so the server routes
        # type=chat messages to us preferentially over any stale
        # patch.* resources still hibernated from prior cold-starts.
        # Priority 5 is the cohort-conventional 'phone' priority —
        # well above the default 0 that those hibernated sessions
        # advertise.
        try:
            from nbxmpp.protocol import Presence
            pres = Presence()
            pres.addChild("priority").addData("5")
            self._client.send_stanza(pres)
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
        # Warm the TURN URI cache so the first call's audio path doesn't
        # have to wait for an XEP-0215 disco round-trip.
        self.get_turn_uris(lambda _uris: None)
        # MAM catch-up on by default — paginated via RSM in pages of
        # MAM_PAGE so the nbxmpp 7.2 large-batch parse issue is
        # sidestepped. PATCH_MAM_CATCHUP=0 to opt OUT (e.g. for debugging).
        import os
        if os.environ.get("PATCH_MAM_CATCHUP", "1") == "1":
            self.request_history_sync(
                all_history=self._settings.get_boolean("sync-all-history"))

    def _on_login_successful(self, _client, _signal_name):
        # Only fires in login-test mode (see _on_connected). Kept as a
        # no-op so the subscribe call doesn't throw if the upstream
        # behaviour ever changes.
        log.debug("login-successful (login-test mode only)")

    def _on_disconnected(self, _client, _signal_name):
        resumable = bool(self._client and self._client.resumeable)
        log.info("disconnected (smacks resumeable=%s)", resumable)
        # Do NOT drop _client here. We want connect_to_server() to
        # re-enter nbxmpp's reconnect path and let smacks <resume/>
        # the session if the server still holds it. Only an explicit
        # disconnect_from_server() or _on_connection_failed clears it.
        # The TURN URI cache also stays — we're on the same XMPP server
        # so the HMAC credentials remain valid for their TTL.
        self._mam_syncing = False
        self._account.set_state(account_mod.STATE_DISCONNECTED)
        self.emit("state-changed", account_mod.STATE_DISCONNECTED)
        if self._want_connected:
            self._fail_count += 1
            self._schedule_reconnect()

    def _on_connection_failed(self, _client, _signal_name):
        self._fail_count += 1
        err = self._client.get_error() if self._client else None
        msg = str(err) if err else "connection failed"
        log.warning("connection failed (#%d): %s", self._fail_count, msg)
        # Connection failure (vs. clean disconnect) means we couldn't
        # establish a TCP/TLS stream. nbxmpp's smacks state may be
        # stale at this point; drop the client so the next retry does
        # a fresh DNS+auth pass instead of retrying resume against an
        # address that's no longer reachable.
        self._client = None
        self._turn_uris = []
        self._turn_uri_fetched_at = 0.0
        self._account.set_state(account_mod.STATE_FAILED, msg)
        self.emit("state-changed", account_mod.STATE_FAILED)
        self._schedule_reconnect()

    def _on_jingle_iq_handler(self, _client, stanza, _properties):
        """Dispatcher-registered handler for `<iq type=set><jingle>`.

        Listening only to the stanza-received signal isn't enough — the
        dispatcher follows the handler chain with a default handler
        that replies <feature-not-implemented/> if nothing claimed the
        iq. That confuses cheogram and ICE never converges. By raising
        NodeProcessed here we tell the dispatcher the iq is handled.
        """
        from nbxmpp.dispatcher import NodeProcessed
        jingle = jingle_mod.parse_jingle(stanza)
        if jingle is None:
            return
        from_jid = stanza.getAttr("from") or ""
        iq_id = stanza.getAttr("id") or ""
        log.info("jingle %s sid=%s from=%s",
                 jingle.get("action"), jingle.get("sid"), from_jid)
        self.emit("jingle-iq", jingle, from_jid, iq_id)
        # ACK and tell the dispatcher we're done so it doesn't fire
        # the feature-not-implemented default reply.
        self._send_iq_result(stanza)
        raise NodeProcessed

    def _on_stanza_received(self, _client, _signal_name, stanza):
        name = stanza.getName()
        # We only handle <message> here. Presence, IQ, etc. flow into
        # nbxmpp's internal dispatcher modules and we can hook those
        # separately if needed.
        if name != "message":
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

        # XEP-0184 delivery receipt — peer (or the cheogram gateway
        # echoing an SMS status report) acknowledging one of OUR sent
        # messages. Body-less, carries <received id='..'/>. Don't fire
        # on MAM replay; the live ack already arrived at the time.
        if not from_mam:
            rec = stanza.getTag("received", namespace=Namespace.RECEIPTS)
            if rec is not None:
                target_id = rec.getAttr("id") or ""
                if target_id:
                    self.emit("message-receipt", target_id, "delivered")
                return

        # XEP-0444 reactions (parsed before the empty-body short-circuit;
        # reactions stanzas are body-less). A <reactions id='..'/> wraps
        # zero or more <reaction/> children; an empty set clears that
        # sender's reactions on the target message.
        reactions_tag = stanza.getTag("reactions", namespace=Namespace.REACTIONS)
        if reactions_tag is not None:
            target_id = reactions_tag.getAttr("id") or ""
            emojis = [r.getData() or "" for r in reactions_tag.getTags("reaction")]
            emojis = [e for e in emojis if e]
            r_from = stanza.getAttr("from") or ""
            if target_id and r_from:
                try:
                    sender_jid = str(JID.from_string(r_from).bare)
                    own_bare = str(JID.from_string(self._account.jid).bare)
                    # Conversation key: if WE sent the reaction (via
                    # carbon or another client), the thread is the
                    # 'to', not the 'from'. Otherwise it's the sender.
                    if sender_jid == own_bare:
                        to_str = stanza.getAttr("to") or ""
                        conv_jid = (str(JID.from_string(to_str).bare)
                                    if to_str else sender_jid)
                    else:
                        conv_jid = sender_jid
                    self.emit("reaction-received", target_id, sender_jid,
                              conv_jid, emojis)
                except Exception:  # noqa: BLE001
                    pass
            return

        # XEP-0085 chat state (parsed before the empty-body short-circuit
        # so bodyless composing/paused notifications surface). May also
        # ride alongside a body — in that case we emit both signals.
        for cs in ("active", "composing", "paused", "inactive", "gone"):
            if stanza.getTag(cs, namespace=Namespace.CHATSTATES) is not None:
                cs_from = stanza.getAttr("from") or ""
                if cs_from:
                    try:
                        cs_bare = str(JID.from_string(cs_from).bare)
                        self.emit("chat-state", cs_bare, cs)
                    except Exception:  # noqa: BLE001
                        pass
                break

        body = stanza.getTagData("body")
        # XEP-0066 Out-of-Band Data — JMP/cheogram attaches MMS image
        # URLs as <x xmlns="jabber:x:oob"><url>...</url></x>. The body
        # typically mirrors the URL but may carry caption text.
        attachment_url = ""
        oob = stanza.getTag("x", namespace=OOB_NS)
        if oob is not None:
            attachment_url = oob.getTagData("url") or ""
        if not body and not attachment_url:
            # Chat state, marker, etc. — nothing to surface.
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
        msg_id = stanza.getAttr("id") or ""

        # XEP-0308 message correction: this stanza REPLACES an earlier
        # one with id=replace_id. Apply MAM replays too; the store update
        # is idempotent, and treating archived corrections as fresh
        # messages creates duplicate bubbles after reconnect.
        replace_el = stanza.getTag("replace", namespace=Namespace.CORRECT)
        if replace_el is not None:
            target_id = replace_el.getAttr("id") or ""
            if target_id:
                self.emit("message-corrected", target_id, bare, body, timestamp)
                return

        # XEP-0461 quoted reply + XEP-0428 fallback handling. The body
        # text we surface to the UI strips the quoted prefix so the
        # bubble shows only the reply text — the reply target lets the
        # renderer pull the original quote-snippet from its own row.
        reply_to_id = ""
        reply_el = stanza.getTag("reply", namespace=Namespace.REPLY)
        if reply_el is not None:
            reply_to_id = reply_el.getAttr("id") or ""
            fb_el = stanza.getTag("fallback", namespace=Namespace.FALLBACK)
            if fb_el is not None and fb_el.getAttr("for") == Namespace.REPLY:
                fb_body = fb_el.getTag("body")
                if fb_body is not None:
                    try:
                        start = int(fb_body.getAttr("start") or "0")
                        end = int(fb_body.getAttr("end") or "0")
                    except ValueError:
                        start, end = 0, 0
                    if end > start:
                        # XEP-0428 indices are utf-8 byte offsets.
                        try:
                            raw = body.encode("utf-8")
                            body = (raw[:start] + raw[end:]).decode(
                                "utf-8", errors="replace").lstrip()
                        except Exception:  # noqa: BLE001
                            pass

        log.info("%smessage %s %s: %s%s",
                 "[mam] " if from_mam else "",
                 "<-" if incoming else "->",
                 bare, body[:80],
                 (" [oob " + attachment_url + "]") if attachment_url else "")
        self.emit("message-received", bare, body, incoming, timestamp,
                  attachment_url, msg_id, reply_to_id)

        # XEP-0184 request — peer wants us to confirm delivery. Reply
        # with <received/> matching the stanza id. Suppress during MAM
        # replay (we'd be ack'ing a message the peer already knows
        # was delivered).
        if incoming and not from_mam and msg_id:
            req = stanza.getTag("request", namespace=Namespace.RECEIPTS)
            if req is not None:
                self._send_receipt(from_str, msg_id)

    # -- XEP-0085 Chat State outbound ------------------------------------

    def send_chat_state(self, to_jid: str, state: str) -> None:
        """Send a standalone chat state notification (composing, paused,
        active, inactive, gone). These are body-less messages with a
        no-store hint so MAM doesn't archive ephemeral typing states."""
        if not self._client or not self._client.is_stream_authenticated:
            return
        msg = Message(to=to_jid, typ="chat")
        msg.addChild(state, namespace=Namespace.CHATSTATES)
        msg.addChild("no-store", namespace=Namespace.HINTS)
        try:
            self._client.send_stanza(msg)
        except Exception:  # noqa: BLE001
            pass

    # -- XEP-0444 Reactions ---------------------------------------------

    def send_reaction(self, to_jid: str, target_msg_id: str,
                      emojis: list[str]) -> bool:
        """Replace our reactions on a message via XEP-0444.

        Cheogram maps these to SMS Tapbacks on the wire — sending '👍'
        for a JMP-routed thread surfaces as a Tapback on the peer's
        SMS client. ``emojis`` is the full new set (not a delta); pass
        an empty list to remove our reactions on that message.
        """
        if not self._client or not self._client.is_stream_authenticated:
            return False
        msg = Message(to=to_jid, typ="chat")
        reactions = msg.addChild("reactions", namespace=Namespace.REACTIONS,
                                 attrs={"id": target_msg_id})
        for e in emojis:
            reactions.addChild("reaction").addData(e)
        # XEP-0334 store hint so the reactions land in MAM and any
        # other client (or our own next session) can replay them.
        msg.addChild("store", namespace=Namespace.HINTS)
        try:
            self._client.send_stanza(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("send_reaction failed: %s", exc)
            return False
        # Local echo so the UI updates without waiting for carbons.
        own_bare = str(JID.from_string(self._account.jid).bare)
        try:
            conv_jid = str(JID.from_string(to_jid).bare)
        except Exception:  # noqa: BLE001
            conv_jid = to_jid
        self.emit("reaction-received", target_msg_id, own_bare,
                  conv_jid, list(emojis))
        return True

    # -- XEP-0191 Blocking ----------------------------------------------

    def block(self, jid: str) -> bool:
        """Block a JID via XEP-0191. Server filters bidirectionally —
        no new messages or presence either way. Returns True if the
        request was dispatched (not the same as 'server applied it');
        callers shouldn't optimistically update local UI on True alone.
        """
        if not self._client or not self._client.is_stream_authenticated:
            return False
        try:
            module = self._client.get_module("Blocking")
            module.block([JID.from_string(jid)])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("block(%s) failed: %s", jid, exc)
            return False

    def unblock(self, jid: str) -> bool:
        if not self._client or not self._client.is_stream_authenticated:
            return False
        try:
            module = self._client.get_module("Blocking")
            module.unblock([JID.from_string(jid)])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("unblock(%s) failed: %s", jid, exc)
            return False

    def _send_receipt(self, to_jid: str, target_id: str) -> None:
        if not self._client or not self._client.is_stream_authenticated:
            return
        ack = Message(to=to_jid, typ="chat")
        ack.addChild("received", namespace=Namespace.RECEIPTS,
                     attrs={"id": target_id})
        ack.addChild("store", namespace=Namespace.HINTS)
        try:
            self._client.send_stanza(ack)
        except Exception as exc:  # noqa: BLE001
            log.debug("send receipt failed: %s", exc)

    # -- iq helpers ------------------------------------------------------

    def _send_iq_result(self, request) -> None:
        if not self._client or not self._client.is_stream_authenticated:
            return
        from_jid = request.getAttr("from") or ""
        iq_id    = request.getAttr("id") or ""
        result = Iq(typ="result", to=from_jid)
        if iq_id:
            result.setAttr("id", iq_id)
        try:
            self._client.send_stanza(result)
        except Exception as exc:  # noqa: BLE001
            log.debug("ack iq failed: %s", exc)

    def send_iq(self, iq: Iq) -> bool:
        if not self._client or not self._client.is_stream_authenticated:
            log.warning("send_iq: not connected")
            return False
        try:
            self._client.send_stanza(iq)
        except Exception as exc:
            log.exception("send_iq failed: %s", exc)
            return False
        return True

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
        # Match Cheogram-Android / cheogram-bot's exact wire shape:
        #   type="chat"  — XEP-0353 examples + most peer impls expect
        #                  chat-typed messages so they route via the
        #                  carbon + offline-storage paths.
        #   <store/>     — XEP-0334 storage hint, so a propose lands in
        #                  the peer's archive even if no resource is on.
        msg = Message(to=peer_jid, typ="chat")
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
        msg.addChild("store", namespace="urn:xmpp:hints")
        try:
            self._client.send_stanza(msg)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_jmi failed: %s", exc)
            return False
        log.info("JMI %s -> id=%s peer=%s", action, session_id, peer_jid)
        return True

    # -- MAM catch-up ----------------------------------------------------

    # MAM (XEP-0313 + XEP-0059 RSM) catch-up. nbxmpp 7.2 chokes on
    # large MAM batches arriving in one TCP read — the SimpleXML parser
    # misinterprets the concatenated byte stream as 'stream finished'
    # mid-blob. Cap each page at MAM_PAGE messages and walk the RSM
    # cursor until complete=True.
    MAM_PAGE = 20

    # When resuming from the latest cached message we rewind the start
    # bound by this much. latest_timestamp() is MAX over all rows
    # including our own outgoing messages, so a peer message that was
    # archived a moment BEFORE one of our sends (e.g. delivered only to
    # another device while this one was off) would otherwise fall before
    # the bound and never be fetched. Re-scanning a short overlap closes
    # that gap; xmpp_id dedup makes the re-fetched rows free.
    MAM_RESUME_OVERLAP = 600  # seconds

    @property
    def mam_sync_active(self) -> bool:
        return self._mam_syncing

    def request_history_sync(self, *, all_history: bool = False) -> bool:
        """Request a paginated MAM sync.

        ``all_history`` omits the MAM start bound and lets the server
        return its full retained archive. Otherwise we resume from the
        latest cached message, falling back to the last day on first run.
        """
        if self._mam_syncing:
            log.info("MAM sync already in progress, skipping")
            return False
        if self._client is None or self._store is None:
            return False
        try:
            mam = self._client.get_module("MAM")
        except Exception as exc:  # noqa: BLE001
            log.warning("MAM module unavailable: %s", exc)
            return False

        import datetime as dt
        if all_history:
            start = None
        else:
            latest = self._store.latest_timestamp()
            if latest > 0:
                start = dt.datetime.fromtimestamp(
                    max(0.0, latest - self.MAM_RESUME_OVERLAP),
                    tz=dt.timezone.utc)
            else:
                # First-ever connect: limit to the last day unless the
                # user explicitly opted into full archive sync.
                start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        own_jid = JID.from_string(self._account.jid)
        if start is None:
            log.info("MAM catch-up from beginning of server archive")
        else:
            log.info("MAM catch-up from %s",
                     start.isoformat(timespec="seconds"))
        # Stash MAM state on self instead of closing over kwargs in a
        # lambda — nbxmpp's add_done_callback uses weak=True by default,
        # and a lambda has no strong owner so the weakref dies before
        # the iq response arrives and the callback never fires.
        self._mam = mam
        self._mam_jid = own_jid.bare
        self._mam_start = start
        self._mam_syncing = True
        self._mam_page = 1
        self._mam_query_page(after=None)
        return True

    def _mam_query_page(self, after: str | None) -> None:
        """Fire one MAM page; the bound-method callback chains to the
        next on incomplete results."""
        try:
            query = {
                "jid": self._mam_jid,
                "queryid": f"patch-catchup-p{self._mam_page}",
                "max_": self.MAM_PAGE,
                "after": after,
                "callback": self._on_mam_page_done,
            }
            if self._mam_start is not None:
                query["start"] = self._mam_start
            self._mam.make_query(**query)
        except Exception as exc:  # noqa: BLE001
            self._mam_syncing = False
            log.warning("MAM page %d failed to dispatch: %s",
                        self._mam_page, exc)

    def _on_mam_page_done(self, task) -> None:
        page = self._mam_page
        try:
            result = task.finish()
        except Exception as exc:  # noqa: BLE001
            self._mam_syncing = False
            log.warning("MAM page %d failed: %s", page, exc)
            return
        complete = bool(getattr(result, "complete", False))
        # rsm.last is the cursor for the NEXT page (per XEP-0059 §3.6).
        rsm = getattr(result, "rsm", None)
        last = getattr(rsm, "last", None) if rsm else None
        log.info("MAM page %d: complete=%s last=%s",
                 page, complete,
                 (last[:12] + "...") if last else last)
        if complete or not last:
            self._mam_syncing = False
            log.info("MAM catch-up done (%d page%s)",
                     page, "" if page == 1 else "s")
            return
        # Schedule the next page on the next idle so the dispatcher has
        # a chance to drain whatever just landed before we ask for more.
        self._mam_page = page + 1
        GLib.idle_add(self._mam_query_page, last)
