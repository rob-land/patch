from __future__ import annotations

import logging
import sys

from gi.repository import Adw, Gio, GLib

from patch import APP_ID
from patch.account import Account
from patch.dialogs.account_dialog import PatchAccountDialog
from patch.logging_setup import configure_logging
from patch.window import PatchWindow

log = logging.getLogger(__name__)


class PatchApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        # `--debug` is consumed by configure_logging via sys.argv, but
        # GApplication needs the flag registered or --help won't list it.
        self.add_main_option(
            "debug", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Enable debug logging", None,
        )

        self._account = Account()

        # App actions
        for name, handler in (
            ("about",       self._show_about),
            ("account",     self._show_account_dialog),
            ("preferences", self._show_account_dialog),  # alias for Phase 0
            ("quit",        lambda *_: self.quit()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.show-help-overlay", ["<Control>question"])

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = PatchWindow(application=self, account=self._account)
        win.present()
        # If the account isn't configured yet, prompt for credentials on
        # first activation. The dialog dismisses cleanly if the user
        # cancels — the rest of the app still works in offline mode.
        if not self._account.is_configured:
            self._show_account_dialog()

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
                ("Built on",    ["JMP.chat", "Prosody", "UnifiedPush"]),
            ),
        )
        about.present(self.props.active_window)


def main(argv=None):
    configure_logging()
    return PatchApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
