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

    def __init__(self, app, account, xmpp, window_provider, focus_provider,
                 contacts=None, calls=None):
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
        self._contacts = contacts

        # GSimpleAction the notification "tap" target activates. The Gio
        # notification machinery routes app.<name> through Gio.Application
        # action lookup, so we just register one app-level action that
        # takes the JID as its parameter.
        action = Gio.SimpleAction.new(
            "open-conversation", GLib.VariantType.new("s"))
        action.connect("activate", self._on_open_conversation)
        self._app.add_action(action)

        self._xmpp.connect("message-received", self._on_message)

        # Missed-call notifications. Fire when an incoming call's terminal
        # state is REJECTED/RETRACTED — i.e. the user didn't pick up
        # (either ignored it or another resource handled it). Tap opens
        # the conversation with the caller so the user can call back or
        # text. We don't fire on STATE_ENDED (active->ended = answered
        # then hung up — that's a completed call, not a missed one).
        if calls is not None:
            calls.connect("call-ended", self._on_call_ended)

    # -- handlers --------------------------------------------------------

    def _on_message(self, _xmpp, remote_jid, body, incoming, _timestamp,
                    _attachment_url, _message_id):
        if not incoming:
            return
        if not body:
            return
        # Suppress when the conversation is already on screen.
        if self._is_focused(remote_jid):
            return
        # Group SMS comes in as "<xmpp:+15551234@cheogram.com> text" —
        # strip the wire prefix and prepend the resolved sender name so
        # the body matches what the user sees inline in the thread.
        sender_jid = None
        if numfmt.is_group_jid(remote_jid):
            sender_jid, body = numfmt.parse_group_body(body)
        self._fire_notification(remote_jid, body, sender_jid)

    def _on_call_ended(self, _manager, session):
        # Only missed-call-shaped terminals: an INCOMING call that the
        # user didn't actively answer.
        if not session.incoming:
            return
        from patch import calls as calls_mod
        if session.state not in (calls_mod.STATE_REJECTED,
                                 calls_mod.STATE_RETRACTED):
            return
        title = session.peer_label or _display_name(
            session.peer_jid, self._account.gateway, self._contacts)
        body = "Missed call"
        notif = Gio.Notification.new(title)
        notif.set_body(body)
        try:
            notif.set_icon(Gio.ThemedIcon.new("call-missed-symbolic"))
        except Exception:  # noqa: BLE001
            pass
        # Tap opens the conversation with the caller.
        notif.set_default_action_and_target(
            "app.open-conversation", GLib.Variant("s", session.peer_jid))
        nid = "patch-missed-" + session.session_id
        self._app.send_notification(nid, notif)
        log.info("missed-call notification: %s", title)

    def _on_open_conversation(self, _action, param):
        jid = param.get_string()
        # Cold-start case: the notification fired from a service-mode
        # instance with no window. Build one by activating the app —
        # that triggers do_activate which constructs PatchWindow.
        win = self._window_provider()
        if win is None:
            self._app.activate()
            win = self._window_provider()
        if win is None:
            log.warning("could not build window for notification tap")
            return
        win.present()
        win.activate_action(
            "win.open-conversation", GLib.Variant("s", jid))

    # -- helpers ---------------------------------------------------------

    def _is_focused(self, remote_jid: str) -> bool:
        win = self._window_provider()
        if win is None or not win.is_active():
            return False
        return self._focus_provider() == remote_jid

    def _fire_notification(self, remote_jid: str, body: str,
                           sender_jid: str | None = None) -> None:
        title = _display_name(remote_jid, self._account.gateway, self._contacts)
        if sender_jid:
            sender_label = (self._contacts.label_for_jid(sender_jid)
                            if self._contacts is not None else sender_jid)
            body = f"{sender_label}: {body}"
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


def _display_name(jid: str, gateway: str, contacts=None) -> str:
    if numfmt.is_group_jid(jid):
        local = jid.partition("@")[0]
        parts = []
        for n in local.split(","):
            name = contacts.lookup(n) if contacts else None
            parts.append(name or numfmt.format_for_display(n))
        return ", ".join(parts)
    number = numfmt.jid_to_number(jid, gateway)
    if number:
        name = contacts.lookup(number) if contacts else None
        return name or numfmt.format_for_display(number)
    return jid


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"
