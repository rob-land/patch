"""New-message entry dialog.

Tiny popup the user gets from the Messages tab's floating-action
button. They type a phone number and tap Open; we normalise to E.164,
build the JID, and route through win.open-conversation — the same
path the dialer's Send Message button uses, so a new conversation
appears in the list once the first message is sent.
"""

from __future__ import annotations

import logging

from gi.repository import Adw, GLib, Gtk

from patch.numfmt import normalize_e164, number_to_jid

log = logging.getLogger(__name__)


@Gtk.Template(resource_path="/land/rob/patch/ui/new-message-dialog.ui")
class PatchNewMessageDialog(Adw.Dialog):
    __gtype_name__ = "PatchNewMessageDialog"

    number_row:  Adw.EntryRow = Gtk.Template.Child()
    open_button: Gtk.Button   = Gtk.Template.Child()

    def __init__(self, account, parent_window):
        super().__init__()
        self._account = account
        self._parent_window = parent_window
        self.open_button.connect("clicked", self._on_open)
        # Pressing Enter in the entry should trigger Open too.
        self.number_row.connect("entry-activated", self._on_open)

    def _on_open(self, *_):
        raw = self.number_row.get_text().strip()
        normalized = normalize_e164(raw, default_country="US")
        if not normalized:
            self.number_row.add_css_class("error")
            return
        jid = number_to_jid(normalized, self._account.gateway)
        self.force_close()
        self._parent_window.activate_action(
            "win.open-conversation", GLib.Variant("s", jid))
