"""Setup dialog for the JMP account.

Adw.PreferencesDialog with JID + password + optional XMPP host fields.
Saves credentials via the Account model (libsecret-backed).
"""

from __future__ import annotations

import logging

from gi.repository import Adw, Gio, Gtk

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/account-dialog.ui")
class PatchAccountDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PatchAccountDialog"

    jid_row:      Adw.EntryRow         = Gtk.Template.Child()
    password_row: Adw.PasswordEntryRow = Gtk.Template.Child()
    host_row:     Adw.EntryRow         = Gtk.Template.Child()
    save_button:  Gtk.Button           = Gtk.Template.Child()
    status_label: Gtk.Label            = Gtk.Template.Child()

    def __init__(self, account):
        super().__init__()
        self._account = account

        # Prefill from the account model. Password gets fetched from
        # libsecret on demand, not bound — we don't want to surface it
        # in property change traffic unless the user opened this dialog.
        self.jid_row.set_text(account.jid or "")
        self.host_row.set_text(account.host or "")
        existing = account.get_password() or ""
        if existing:
            self.password_row.set_text(existing)

        # Local action group: the save button's action-name is
        # "patch.save-account". One click = save + (later) attempt connect.
        actions = Gio.SimpleActionGroup()
        save_action = Gio.SimpleAction.new("save-account", None)
        save_action.connect("activate", self._on_save)
        actions.add_action(save_action)
        self.insert_action_group("patch", actions)

    def _on_save(self, *_):
        jid = self.jid_row.get_text().strip()
        password = self.password_row.get_text()
        host = self.host_row.get_text().strip()

        if not jid or "@" not in jid:
            self._set_status(_msg="Enter a JID like rob@example.org or your JMP number.")
            return
        if not password:
            self._set_status(_msg="Enter the account password.")
            return

        ok = self._account.save(jid, password, host)
        if not ok:
            self._set_status(
                _msg="Could not store credentials in the system keyring."
            )
            return
        # Kick off the connection. The window's status banner mirrors
        # the account state machine so the user sees connecting → online
        # (or the error) without us having to babysit a status_label
        # update here.
        if app := Gio.Application.get_default():
            app.activate_action("connect", None)
        self._set_status(_msg="Saved. Connecting…")

    def _set_status(self, _msg: str) -> None:
        self.status_label.set_text(_msg)
