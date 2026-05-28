"""Setup dialog for the JMP account.

Adw.PreferencesDialog with JID + password + optional XMPP host fields.
Saves credentials via the Account model (libsecret-backed).
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gdk, Gio, Gtk

from patch import account as account_mod

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/account-dialog.ui")
class PatchAccountDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PatchAccountDialog"

    jid_row:              Adw.EntryRow         = Gtk.Template.Child()
    password_row:         Adw.PasswordEntryRow = Gtk.Template.Child()
    host_row:             Adw.EntryRow         = Gtk.Template.Child()
    jid_paste_button:     Gtk.Button           = Gtk.Template.Child()
    password_paste_button: Gtk.Button          = Gtk.Template.Child()
    save_button:          Gtk.Button           = Gtk.Template.Child()
    status_label:         Gtk.Label            = Gtk.Template.Child()

    def __init__(self, account):
        super().__init__()
        self._account = account
        # Set once Save has been pressed — gates whether we mirror the
        # account state machine into status_label. Without this gate the
        # dialog would echo any background state change the moment it
        # opens, drowning out the initial "password missing" hint.
        self._save_attempted = False

        # Prefill from the account model. Password gets fetched from
        # libsecret on demand, not bound — we don't want to surface it
        # in property change traffic unless the user opened this dialog.
        self.jid_row.set_text(account.jid or "")
        self.host_row.set_text(account.host or "")
        existing = account.get_password() or ""
        if existing:
            self.password_row.set_text(existing)
        elif account.is_configured:
            # JID is remembered but the keyring has no password —
            # tell the user up front why we surfaced the dialog.
            self._set_status("Password missing from the keyring — re-enter it above.")

        # Local action group: the save button's action-name is
        # "patch.save-account". One click = save + attempt connect.
        actions = Gio.SimpleActionGroup()
        save_action = Gio.SimpleAction.new("save-account", None)
        save_action.connect("activate", self._on_save)
        actions.add_action(save_action)
        self.insert_action_group("patch", actions)

        # Mirror the account state into status_label so the user sees
        # connecting → connected (or failed, with the error) instead of
        # the label being stuck on "Saved. Connecting…".
        self._state_handler = account.connect("notify::state",
                                              self._on_account_state_changed)
        self._error_handler = account.connect("notify::last-error",
                                              self._on_account_state_changed)
        self.connect("closed", self._on_closed)

        self.jid_paste_button.connect("clicked",
            lambda *_: self._paste_into(self.jid_row))
        self.password_paste_button.connect("clicked",
            lambda *_: self._paste_into(self.password_row))

    def _on_save(self, *_):
        jid = self.jid_row.get_text().strip()
        password = self.password_row.get_text()
        host = self.host_row.get_text().strip()

        if not jid or "@" not in jid:
            self._set_status("Enter a JID like rob@example.org or your JMP number.")
            return
        if not password:
            self._set_status("Enter the account password.")
            return

        ok = self._account.save(jid, password, host)
        if not ok:
            self._set_status("Could not store credentials in the system keyring.")
            return
        self._save_attempted = True
        self._set_status("Saved. Connecting…")
        # The connect action will flip account state to CONNECTING and
        # then CONNECTED/FAILED — _on_account_state_changed picks that
        # up and replaces this status text.
        if app := Gio.Application.get_default():
            app.activate_action("connect", None)

    def _on_account_state_changed(self, _account, _pspec):
        if not self._save_attempted:
            return
        state = self._account.state
        if state == account_mod.STATE_CONNECTING:
            self._set_status("Connecting…")
        elif state == account_mod.STATE_CONNECTED:
            self._set_status("Connected.")
        elif state == account_mod.STATE_FAILED:
            self._set_status(self._account.last_error or "Connection failed.")
        elif state == account_mod.STATE_DISCONNECTED:
            self._set_status("Disconnected.")

    def _on_closed(self, *_):
        if self._state_handler:
            self._account.disconnect(self._state_handler)
            self._state_handler = 0
        if self._error_handler:
            self._account.disconnect(self._error_handler)
            self._error_handler = 0

    def _paste_into(self, row) -> None:
        display = self.get_display() or Gdk.Display.get_default()
        if display is None:
            return
        clipboard = display.get_clipboard()
        clipboard.read_text_async(None, self._on_paste_ready, row)

    def _on_paste_ready(self, clipboard, result, row):
        try:
            text = clipboard.read_text_finish(result)
        except Exception:  # noqa: BLE001
            return
        if text:
            row.set_text(text.strip())

    def _set_status(self, msg: str) -> None:
        self.status_label.set_text(msg)
