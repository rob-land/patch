"""Preferences dialog — top-level settings surface.

Distinct from the Account dialog (which is the narrow JID + password
form). Preferences exposes connection diagnostics, a calls page, and
a "Blocked" page where the user can view + unblock numbers.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Adw, GLib, Gio, Gtk

from patch import APP_ID
from patch.logging_setup import log_path

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/preferences-dialog.ui")
class PatchPreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "PatchPreferencesDialog"

    distributor_row: Adw.ActionRow = Gtk.Template.Child()
    endpoint_row:    Adw.ActionRow = Gtk.Template.Child()
    log_path_row:    Adw.ActionRow = Gtk.Template.Child()
    blocked_group:   Adw.PreferencesGroup = Gtk.Template.Child()
    sync_all_history_row: Adw.SwitchRow = Gtk.Template.Child()

    def __init__(self, xmpp=None, account=None):
        super().__init__()
        self._xmpp = xmpp
        self._account = account
        self._settings = Gio.Settings.new(APP_ID)
        dist = self._settings.get_string("push-distributor") \
            or "(none configured)"
        ep = self._settings.get_string("push-endpoint") \
            or "(not registered yet)"
        self.distributor_row.set_subtitle(dist)
        self.endpoint_row.set_subtitle(ep)
        self.log_path_row.set_subtitle(log_path())
        self._settings.bind(
            "sync-all-history", self.sync_all_history_row, "active",
            Gio.SettingsBindFlags.DEFAULT)
        GLib.idle_add(self._fetch_block_list)

    def _fetch_block_list(self) -> bool:
        if self._xmpp is None or self._xmpp._client is None:
            self._add_blocked_placeholder("Not connected")
            return False
        try:
            module = self._xmpp._client.get_module("Blocking")
            task = module.request_blocking_list()
            task.add_done_callback(self._on_block_list)
        except Exception as exc:  # noqa: BLE001
            self._add_blocked_placeholder(f"Could not fetch: {exc}")
        return False

    def _on_block_list(self, task):
        try:
            blocked = task.finish()
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._add_blocked_placeholder,
                          f"Failed: {exc}")
            return
        GLib.idle_add(self._populate_blocked, blocked)

    def _populate_blocked(self, blocked_jids) -> bool:
        if not blocked_jids:
            self._add_blocked_placeholder("No blocked contacts")
            return False
        from patch import numfmt
        for jid in blocked_jids:
            jid_str = str(jid)
            gateway = (self._account.gateway
                       if self._account is not None else "cheogram.com")
            number = numfmt.jid_to_number(jid_str, gateway)
            display = numfmt.format_for_display(number) if number else jid_str
            row = Adw.ActionRow(title=display)
            row.set_use_markup(False)
            btn = Gtk.Button(label="Unblock", valign=Gtk.Align.CENTER)
            btn.add_css_class("destructive-action")
            btn.connect("clicked", self._on_unblock, jid_str, row)
            row.add_suffix(btn)
            self.blocked_group.add(row)
        return False

    def _on_unblock(self, _btn, jid_str, row):
        if self._xmpp is not None and self._xmpp.unblock(jid_str):
            self.blocked_group.remove(row)

    def _add_blocked_placeholder(self, text: str) -> bool:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("dim-label")
        label.set_margin_start(16)
        label.set_margin_top(8)
        self.blocked_group.add(label)
        return False
