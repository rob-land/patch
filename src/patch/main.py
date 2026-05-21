from __future__ import annotations

import logging
import sys

from gi.repository import Adw, Gio, GLib

from patch import APP_ID
from patch import account as account_mod
from patch.account import Account
from patch.dialogs.account_dialog import PatchAccountDialog
from patch.logging_setup import configure_logging
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
        self._account = Account()
        self._store   = MessageStore()
        self._xmpp    = XmppClient(self._account)
        self._push    = PushController(self._account, self._xmpp)

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

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = PatchWindow(application=self,
                              account=self._account,
                              store=self._store,
                              xmpp=self._xmpp)
            # Kick off push only after the window exists — Connector1
            # publish + Register calls need the session bus + GLib loop
            # both running, which happens by the time present() returns.
            self._push.start()
        win.present()
        if not self._account.is_configured:
            self._show_account_dialog()
        else:
            # Kick off the connection on first activation. State changes
            # propagate via the notify::state handler.
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
