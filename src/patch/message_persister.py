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

    def _on_message(self, _xmpp, remote_jid, body, incoming, timestamp,
                    attachment_url):
        sender_jid = None
        if numfmt.is_group_jid(remote_jid):
            sender_jid, body = numfmt.parse_group_body(body)
        try:
            self._store.add_message(
                remote_jid, bool(incoming), body, timestamp, sender_jid,
                attachment_url=attachment_url or None)
        except Exception as exc:  # noqa: BLE001
            log.warning("persist failed for %s: %s", remote_jid, exc)
