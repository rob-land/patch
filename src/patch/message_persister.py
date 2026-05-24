"""Always-on subscriber that persists inbound + locally-echoed messages
to the SQLite store.

Originally the messages page itself did the store.add_message() write
from its message-received handler. That worked when the user had the
window open, but in `--gapplication-service` cold-start (push wake),
the window is never constructed and the page isn't either — so a
new SMS surfaced as a notification, fired no traceback, and quietly
failed to land in the database.

This class lives on the Application, subscribes to message-received
unconditionally, and handles the JMP-specific group-SMS body parsing
that the store needs to fill `sender_jid` correctly.
"""

from __future__ import annotations

import logging

from patch import numfmt

log = logging.getLogger(__name__)


class MessagePersister:
    def __init__(self, xmpp, store):
        self._xmpp = xmpp
        self._store = store
        self._xmpp.connect("message-received", self._on_message)
        self._xmpp.connect("message-receipt",  self._on_receipt)
        self._xmpp.connect("reaction-received", self._on_reaction)

    def _on_message(self, _xmpp, remote_jid, body, incoming, timestamp,
                    attachment_url, message_id, reply_to_id):
        sender_jid = None
        if numfmt.is_group_jid(remote_jid):
            sender_jid, body = numfmt.parse_group_body(body)
        # Persist the stanza id for BOTH directions so reactions can
        # reference inbound messages too. delivery_state is the only
        # outgoing-specific field.
        xmpp_id = message_id or None
        delivery_state = "sent" if not incoming else None
        try:
            self._store.add_message(
                remote_jid, bool(incoming), body, timestamp, sender_jid,
                attachment_url=attachment_url or None,
                xmpp_id=xmpp_id, delivery_state=delivery_state,
                reply_to_id=reply_to_id or None)
        except Exception as exc:  # noqa: BLE001
            log.warning("persist failed for %s: %s", remote_jid, exc)

    def _on_receipt(self, _xmpp, message_id, new_state):
        try:
            self._store.set_delivery_state(message_id, new_state)
        except Exception as exc:  # noqa: BLE001
            log.warning("receipt persist failed for %s: %s", message_id, exc)

    def _on_reaction(self, _xmpp, target_msg_id, sender_jid, _conv_jid, emojis):
        try:
            self._store.set_reactions(target_msg_id, sender_jid, list(emojis))
        except Exception as exc:  # noqa: BLE001
            log.warning("reaction persist failed for %s: %s",
                        target_msg_id, exc)
