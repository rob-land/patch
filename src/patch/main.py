from __future__ import annotations

import logging
import sys

from gi.repository import Adw, Gio, GLib

from patch import APP_ID
from patch import account as account_mod
from patch.account import Account
from patch.calls import CallManager
from patch.contacts import ContactsManager
from patch.dialogs.account_dialog import PatchAccountDialog
from patch.dialogs.call_dialog import PatchCallDialog
from patch.logging_setup import configure_logging
from patch.notifications import NotificationManager
from patch.push.controller import PushController
from patch.store.db import MessageStore
from patch.window import PatchWindow
from patch.xmpp.client import XmppClient

log = logging.getLogger(__name__)


class PatchApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.add_main_option(
            "debug", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Enable debug logging", None,
        )

        # Construct the long-lived services. These outlive the window so
        # restarts/reactivations don't tear down the XMPP stream.
        self._account       = Account()
        self._store         = MessageStore()
        self._xmpp          = XmppClient(self._account, store=self._store)
        self._contacts      = ContactsManager(self._account)
        self._calls         = CallManager(self._account, self._xmpp,
                                          contacts=self._contacts,
                                          store=self._store)
        self._push          = PushController(self._account, self._xmpp)
        # Auto-present the call dialog when a new session starts. Lives
        # on the Application (not the window) so cold-start activated
        # incoming calls have a screen even before the window is built.
        self._calls.connect("call-started", self._on_call_started)
        # Publish the Connector1 D-Bus object NOW, before Adw.Application
        # registers the `land.rob.patch` bus name in do_startup. dbus-daemon
        # holds queued Message calls until the name is acquired and then
        # dispatches them — if Connector1 isn't registered on the bus
        # connection at that exact moment, the queued cold-start push call
        # errors with "no such interface" and the push is lost. Publishing
        # the object eagerly closes that race.
        self._push.publish_connector()
        # NotificationManager talks to the window+messages page through
        # callable hooks because they aren't constructed yet (lifecycle
        # is application -> startup -> activate -> window).
        self._notifications = NotificationManager(
            self, self._account, self._xmpp,
            window_provider=lambda: self.props.active_window,
            focus_provider=self._focused_jid,
            contacts=self._contacts,
        )

        for name, handler in (
            ("about",       self._show_about),
            ("account",     self._show_account_dialog),
            ("preferences", self._show_account_dialog),
            ("connect",     lambda *_: self._xmpp.connect_to_server()),
            ("disconnect",  lambda *_: self._xmpp.disconnect_from_server()),
            ("quit",        lambda *_: self.quit()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.show-help-overlay", ["<Control>question"])

        # When the user finishes saving credentials, the account state
        # flips to DISCONNECTED — that's our cue to try a connect. This
        # also fires after a manual app.disconnect → reconnect flow.
        self._account.connect("notify::state", self._on_account_state)

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Connector1 already lives on the bus (see __init__). Now that the
        # bus name is owned, kick off the registration with the configured
        # distributor. Safe in both the foreground launch path and the
        # --gapplication-service cold-start activated by dbus-daemon.
        self._push.start_registration()
        # Folks aggregator prep — index builds asynchronously and the
        # UI re-renders once contacts-index-changed fires.
        self._contacts.start()

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = PatchWindow(application=self,
                              account=self._account,
                              store=self._store,
                              xmpp=self._xmpp,
                              calls=self._calls,
                              contacts=self._contacts)
        win.present()
        if not self._account.is_configured:
            self._show_account_dialog()
        else:
            self._xmpp.connect_to_server()

    def _on_account_state(self, account, _pspec):
        state = account.state
        log.debug("account state -> %s", state)
        if state == account_mod.STATE_DISCONNECTED and account.is_configured:
            # Don't autoreconnect on a user-initiated disconnect; let the
            # explicit `app.connect` action drive it. For Phase 1 the
            # only DISCONNECTED entry-point is "credentials just saved",
            # so it's safe to connect here.
            self._xmpp.connect_to_server()

    def _show_account_dialog(self, *_):
        dialog = PatchAccountDialog(self._account)
        dialog.present(self.props.active_window)

    def _on_call_started(self, _manager, session, _direction):
        win = self.props.active_window
        if win is None:
            # Incoming call on a cold-started instance — bring the window
            # up so the dialog has a parent to attach to.
            self.activate()
            win = self.props.active_window
        if win is None:
            return
        dialog = PatchCallDialog(self._calls, session)
        dialog.present(win)

    def _focused_jid(self):
        win = self.props.active_window
        if win is None:
            return None
        # The window exposes a helper that proxies to the messages page.
        getter = getattr(win, "messages_focused_jid", None)
        return getter() if getter else None

    def _show_about(self, *_):
        from patch import APP_NAME, VERSION
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Rob",
            version=VERSION,
            license_type=__import__("gi").repository.Gtk.License.GPL_3_0,
            comments=(
                "JMP.chat-first phone client for GNOME mobile. Built around "
                "self-hosted UnifiedPush via Prosody's "
                "mod_cloud_notify_unifiedpush."
            ),
            issue_url="https://codeberg.org/robland/patch/issues",
            credits_section=(
                ("Inspired by", ["Cheogram on Android — https://cheogram.com"]),
                ("Built on",    ["JMP.chat", "Prosody", "UnifiedPush", "nbxmpp"]),
            ),
        )
        about.present(self.props.active_window)


def main(argv=None):
    configure_logging()
    return PatchApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
