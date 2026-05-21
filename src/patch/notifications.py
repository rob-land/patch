"""Desktop notifications on inbound messages.

Listens to the XmppClient's `message-received` signal. When a message is
inbound *and* its conversation isn't currently in focus (Messages tab
visible + that thread open + window not hidden), fires a Gio.Notification
that opens the right thread when clicked.

The "in focus" check lives here (rather than in messages.py) because the
gating logic is the only thing the rest of the app cares about — the
messages page just exposes a small predicate for which thread is open.
"""

from __future__ import annotations

import logging

from gi.repository import Gio, GLib

from patch import APP_ID
from patch import numfmt

log = logging.getLogger(__name__)


class NotificationManager:
    """Bridges XMPP message arrival to Gio.Notification with sensible gating."""

    def __init__(self, app, account, xmpp, window_provider, focus_provider):
        """Constructor.

        Arguments
        ---------
        app             Adw.Application. Used for send_notification().
        account         the Account model, for the gateway domain (display).
        xmpp            XmppClient, the source of message-received.
        window_provider zero-arg callable returning the active window
                        (or None if the app is fully background-only).
        focus_provider  zero-arg callable returning the currently-open
                        thread JID (str), or None if no thread is open
                        or the messages tab isn't visible.
        """
        self._app = app
        self._account = account
        self._xmpp = xmpp
        self._window_provider = window_provider
        self._focus_provider = focus_provider

        # GSimpleAction the notification "tap" target activates. The Gio
        # notification machinery routes app.<name> through Gio.Application
        # action lookup, so we just register one app-level action that
        # takes the JID as its parameter.
        action = Gio.SimpleAction.new(
            "open-conversation", GLib.VariantType.new("s"))
        action.connect("activate", self._on_open_conversation)
        self._app.add_action(action)

        self._xmpp.connect("message-received", self._on_message)

    # -- handlers --------------------------------------------------------

    def _on_message(self, _xmpp, remote_jid, body, incoming, _timestamp,
                    _attachment_url):
        if not incoming:
            return
        if not body:
            return
        # Suppress when the conversation is already on screen.
        if self._is_focused(remote_jid):
            return
        self._fire_notification(remote_jid, body)

    def _on_open_conversation(self, _action, param):
        jid = param.get_string()
        # Surface the window first so present() does something visible.
        win = self._window_provider()
        if win is not None:
            win.present()
        # Then ask the app to route to the conversation — the messages
        # page picks this up via a window-scoped action so it can switch
        # to the right tab + push the thread page.
        if win is not None:
            win.activate_action(
                "win.open-conversation", GLib.Variant("s", jid))

    # -- helpers ---------------------------------------------------------

    def _is_focused(self, remote_jid: str) -> bool:
        win = self._window_provider()
        if win is None or not win.is_active():
            return False
        return self._focus_provider() == remote_jid

    def _fire_notification(self, remote_jid: str, body: str) -> None:
        title = _display_name(remote_jid, self._account.gateway)
        notif = Gio.Notification.new(title)
        notif.set_body(_truncate(body, 240))
        # icon: hint at messages rather than a generic app icon
        try:
            notif.set_icon(Gio.ThemedIcon.new("user-available-symbolic"))
        except Exception:  # noqa: BLE001
            pass
        notif.set_default_action_and_target(
            "app.open-conversation", GLib.Variant("s", remote_jid))
        # Stable notification id per conversation so newer messages from
        # the same sender replace, not stack, the notification.
        nid = "patch-msg-" + remote_jid
        self._app.send_notification(nid, notif)
        log.debug("notification fired for %s", remote_jid)


def _display_name(jid: str, gateway: str) -> str:
    if numfmt.is_group_jid(jid):
        local = jid.partition("@")[0]
        return ", ".join(numfmt.format_for_display(n) for n in local.split(","))
    number = numfmt.jid_to_number(jid, gateway)
    if number:
        return numfmt.format_for_display(number)
    return jid


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"
